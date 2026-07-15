from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from z_apply_core.browser_session import (
    BrowserSession,
    BrowserToolExecutionError,
    SubmitControlKind,
)


class BrowserSubmissionGuardTests(unittest.IsolatedAsyncioTestCase):
    def _session(
        self,
        *,
        is_submit: bool,
        submit_targets: set[str] | None = None,
    ) -> tuple[BrowserSession, AsyncMock]:
        targets = submit_targets

        async def resolve_target(*, target: str) -> SimpleNamespace:
            submit = target in targets if targets is not None else is_submit
            submit_control = SimpleNamespace(
                evaluate=AsyncMock(return_value=submit),
                click=AsyncMock(),
            )
            handle = SimpleNamespace(
                as_element=lambda: submit_control,
                dispose=AsyncMock(),
            )
            locator = SimpleNamespace(
                evaluate=AsyncMock(return_value=submit),
                evaluate_handle=AsyncMock(return_value=handle),
            )
            return SimpleNamespace(locator=locator)

        tab = SimpleNamespace(
            resolve_target=AsyncMock(side_effect=resolve_target)
        )

        async def call_backend(name: str, *_args: object, **_kwargs: object) -> str:
            return "review state" if name == "browser_snapshot" else "clicked"

        backend = SimpleNamespace(
            _ensure_tab=AsyncMock(return_value=tab),
            call_tool=AsyncMock(side_effect=call_backend),
        )
        session = object.__new__(BrowserSession)
        session._backend = backend
        session.run_id = "guard-test"
        session._capture_workspace = Path("/tmp/guard-test")
        session._submission_guard_active = False
        session._submission_capability = None
        session._last_snapshot = "review state"
        session._last_observation = None
        session._browser_revision = 0
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]
        return session, backend.call_tool

    async def _approve(self, session: BrowserSession, target: str = "e10") -> None:
        await session.prepare_submission_review(target, "Reviewed candidate values")
        session.set_submit_approval(True)

    async def test_submit_control_is_blocked_before_browser_mutation(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session.activate_submission_guard()

        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.call_tool("browser_click", {"target": "e10"})

        call_tool.assert_not_awaited()

    async def test_file_upload_trigger_click_is_rejected_before_native_chooser(self) -> None:
        session, call_tool = self._session(is_submit=False)
        session._is_file_upload_trigger = AsyncMock(return_value=True)  # type: ignore[method-assign]

        with self.assertRaisesRegex(BrowserToolExecutionError, "Native file chooser"):
            await session.call_tool("browser_click", {"target": "e-upload"})

        call_tool.assert_not_awaited()

    async def test_ordinary_control_is_not_rejected_because_form_has_file_input(self) -> None:
        session, call_tool = self._session(is_submit=False)
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]

        self.assertEqual(
            await session.call_tool("browser_click", {"target": "e-continue"}),
            "clicked",
        )

        call_tool.assert_awaited_once()

    async def test_approval_allows_exactly_one_successful_submit(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session.activate_submission_guard()
        await self._approve(session)

        self.assertEqual(
            await session.call_tool("browser_click", {"target": "e10"}),
            "clicked",
        )
        self.assertEqual(call_tool.await_count, 2)

        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.call_tool("browser_click", {"target": "e10"})

    async def test_reversible_click_does_not_consume_submit_approval(self) -> None:
        session, _ = self._session(is_submit=False, submit_targets={"e10"})
        session.activate_submission_guard()
        await self._approve(session)

        await session.call_tool("browser_click", {"target": "e5"})

        capability = session.submission_capability
        self.assertIsNotNone(capability)
        self.assertFalse(capability.consumed if capability is not None else True)

    async def test_structural_search_submit_is_not_treated_as_final_application(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session.activate_submission_guard()
        session._classify_submit_control = AsyncMock(  # type: ignore[method-assign]
            return_value=SubmitControlKind.REVERSIBLE_SEARCH
        )

        self.assertEqual(
            await session.call_tool("browser_click", {"target": "e-search"}),
            "clicked",
        )
        call_tool.assert_awaited_once()

    async def test_typing_with_submit_is_guarded_without_dom_text_matching(self) -> None:
        session, call_tool = self._session(is_submit=False)
        session.activate_submission_guard()

        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.call_tool(
                "browser_type",
                {"target": "e5", "text": "candidate", "submit": True},
            )

        call_tool.assert_not_awaited()

    async def test_structurally_verified_auth_submit_bypasses_application_lock(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session.activate_submission_guard()

        evidence = await session.submit_auth_form("e10")

        self.assertEqual(call_tool.await_count, 1)
        self.assertEqual(call_tool.await_args_list[0].args[0], "browser_snapshot")
        self.assertIn("review state", evidence)
        self.assertIsNone(session.submission_capability)

    async def test_component_auth_scope_can_submit_without_native_form(self) -> None:
        session, call_tool = self._session(is_submit=True)

        evidence = await session.submit_auth_form("e10")

        self.assertIn("review state", evidence)
        self.assertEqual(call_tool.await_args_list[0].args[0], "browser_snapshot")

    async def test_auth_submit_classifies_pointer_interception_as_recoverable(self) -> None:
        session, call_tool = self._session(is_submit=True)
        tab = session._backend._ensure_tab.return_value
        submit_control = SimpleNamespace(
            evaluate=AsyncMock(return_value=True),
            click=AsyncMock(side_effect=TimeoutError("pointer interception")),
        )
        handle = SimpleNamespace(
            as_element=lambda: submit_control,
            dispose=AsyncMock(),
        )
        locator = SimpleNamespace(
            evaluate=AsyncMock(return_value=True),
            evaluate_handle=AsyncMock(return_value=handle),
        )
        tab.resolve_target.side_effect = None
        tab.resolve_target.return_value = SimpleNamespace(locator=locator)

        with self.assertRaisesRegex(
            BrowserToolExecutionError,
            "recoverable browser actionability state",
        ):
            await session.submit_auth_form("e10")

        call_tool.assert_not_awaited()

    async def test_approval_is_revoked_when_reviewed_page_changes(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session.activate_submission_guard()
        await self._approve(session)
        call_tool.side_effect = ["changed review state"]

        with self.assertRaisesRegex(BrowserToolExecutionError, "was revoked"):
            await session.call_tool("browser_click", {"target": "e10"})

        self.assertIsNone(session.submission_capability)

    async def test_approval_rejects_a_different_submit_target(self) -> None:
        session, call_tool = self._session(
            is_submit=False,
            submit_targets={"e10", "e11"},
        )
        session.activate_submission_guard()
        await self._approve(session, "e10")

        with self.assertRaisesRegex(BrowserToolExecutionError, "exact current submit"):
            await session.call_tool("browser_click", {"target": "e11"})

        call_tool.assert_not_awaited()

    async def test_non_auth_form_cannot_use_auth_submit_path(self) -> None:
        session, call_tool = self._session(is_submit=False)
        session.activate_submission_guard()

        with self.assertRaisesRegex(
            BrowserToolExecutionError,
            "structurally identifiable login",
        ):
            await session.submit_auth_form("e10")

        call_tool.assert_not_awaited()

    async def test_stale_submit_target_becomes_recoverable_browser_error(self) -> None:
        session, call_tool = self._session(is_submit=False)
        session.activate_submission_guard()
        session._backend._ensure_tab.return_value.resolve_target.side_effect = ValueError(
            "stale ref"
        )

        with self.assertRaisesRegex(
            BrowserToolExecutionError,
            "capture a fresh snapshot",
        ):
            await session.call_tool("browser_click", {"target": "e510"})

        call_tool.assert_not_awaited()

    async def test_stale_auth_target_becomes_recoverable_browser_error(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session._backend._ensure_tab.return_value.resolve_target.side_effect = ValueError(
            "stale ref"
        )

        with self.assertRaisesRegex(
            BrowserToolExecutionError,
            "recoverable browser actionability state",
        ):
            await session.submit_auth_form("e510")

        call_tool.assert_not_awaited()

    async def test_temporary_verification_tab_is_closed_and_original_restored(self) -> None:
        session, _ = self._session(is_submit=True)
        tabs: list[object] = []
        context = SimpleNamespace()
        original = SimpleNamespace(
            context=context,
            capture_snapshot=AsyncMock(return_value="original application evidence"),
        )
        temporary = SimpleNamespace(
            check_url_and_navigate=AsyncMock(),
            page=SimpleNamespace(title=AsyncMock(return_value="Account verified")),
            capture_snapshot=AsyncMock(return_value="verification succeeded"),
        )
        tabs.extend([original, temporary])

        async def close_temporary() -> None:
            tabs.remove(temporary)

        temporary.close = AsyncMock(side_effect=close_temporary)
        context.new_tab = AsyncMock(return_value=temporary)
        context.tabs = lambda: tabs
        context.select_tab = AsyncMock()
        session._backend._ensure_tab.return_value = original

        result = await session.open_verification_link("https://example.com/verify")

        temporary.check_url_and_navigate.assert_awaited_once()
        temporary.close.assert_awaited_once()
        context.select_tab.assert_awaited_once_with(0)
        self.assertEqual(tabs, [original])
        self.assertIn("VERIFICATION_TAB_COMPLETED_AND_CLOSED", result)
        self.assertIn("original application evidence", result)


if __name__ == "__main__":
    unittest.main()
