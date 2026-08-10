from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from z_apply_core.browser_observation import BrowserCapabilities
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
            submit_control = SimpleNamespace(
                click=AsyncMock(),
            )
            return SimpleNamespace(locator=submit_control)

        page = MagicMock()
        tab = SimpleNamespace(
            page=page,
            resolve_target=AsyncMock(side_effect=resolve_target),
            liveness=AsyncMock(return_value=True),
        )

        async def call_backend(name: str, *_args: object, **_kwargs: object) -> str:
            return "review state" if name == "browser_snapshot" else "clicked"

        backend = SimpleNamespace(
            _ensure_tab=AsyncMock(return_value=tab),
            call_tool=AsyncMock(side_effect=call_backend),
        )
        session = BrowserSession(
            None,
            run_id="guard-test",
            backend=backend,
            tools=[],
            owns_backend=False,
        )
        session._last_snapshot = "review state"
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]
        session._classify_submit_control = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda arguments: (
                SubmitControlKind.FORM_SUBMIT
                if (arguments.get("target") in targets if targets is not None else is_submit)
                else SubmitControlKind.NOT_SUBMIT
            )
        )
        session.inspect_capabilities = AsyncMock(  # type: ignore[method-assign]
            return_value=BrowserCapabilities(enabled_form_submit_visible=True)
        )
        return session, backend.call_tool

    async def _approve(self, session: BrowserSession) -> None:
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
        # The approved click is one layer call: gate + liveness + click, no
        # pre-click snapshot/identity/capabilities round-trips.
        self.assertEqual(call_tool.await_count, 1)

        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.call_tool("browser_click", {"target": "e10"})

    async def test_reversible_click_does_not_consume_submit_approval(self) -> None:
        session, call_tool = self._session(is_submit=False, submit_targets={"e10"})
        session.activate_submission_guard()
        await self._approve(session)

        await session.call_tool("browser_click", {"target": "e5"})
        await session.call_tool("browser_click", {"target": "e10"})

        self.assertEqual(call_tool.await_count, 2)

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

        locator = (
            await session._backend._ensure_tab.return_value.resolve_target(target="e10")
        ).locator
        with patch(
            "z_apply_core.browser_session.resolve_auth_submit_control",
            new=AsyncMock(return_value=locator),
        ):
            evidence = await session.submit_auth_form("e10")

        self.assertEqual(call_tool.await_count, 1)
        self.assertEqual(call_tool.await_args_list[0].args[0], "browser_snapshot")
        self.assertIn("review state", evidence)

    async def test_auth_submit_classifies_pointer_interception_as_recoverable(self) -> None:
        session, call_tool = self._session(is_submit=True)
        tab = session._backend._ensure_tab.return_value
        submit_control = SimpleNamespace(
            click=AsyncMock(side_effect=TimeoutError("pointer interception")),
        )
        tab.resolve_target.side_effect = None
        tab.resolve_target.return_value = SimpleNamespace(locator=submit_control)

        with (
            patch(
                "z_apply_core.browser_session.resolve_auth_submit_control",
                new=AsyncMock(return_value=submit_control),
            ),
            self.assertRaisesRegex(
                BrowserToolExecutionError,
                "recoverable browser actionability state",
            ),
        ):
            await session.submit_auth_form("e10")

        call_tool.assert_not_awaited()

    async def test_approval_clicks_without_pre_click_verification_round_trips(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session.activate_submission_guard()
        await self._approve(session)

        self.assertEqual(
            await session.call_tool("browser_click", {"target": "e10"}),
            "clicked",
        )
        # No snapshot/identity/capabilities calls before the click; the gate +
        # liveness probe + one layer click call.
        self.assertEqual(call_tool.await_count, 1)
        self.assertEqual(call_tool.await_args.args[0], "browser_click")

    async def test_approval_arms_any_current_form_submit_control(self) -> None:
        session, call_tool = self._session(
            is_submit=False,
            submit_targets={"e10", "e11"},
        )
        session.activate_submission_guard()
        await self._approve(session)

        # Approval binds to the application, not a DOM ref: the runtime
        # re-resolves the submit control from live DOM at click time, so a
        # re-rendered control ref still submits exactly once.
        self.assertEqual(
            await session.call_tool("browser_click", {"target": "e11"}),
            "clicked",
        )
        call_tool.assert_awaited_once()

        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.call_tool("browser_click", {"target": "e11"})

    async def test_submit_approved_application_clicks_under_armed_guard(self) -> None:
        session, call_tool = self._session(is_submit=True, submit_targets={"e10"})
        session.activate_submission_guard()
        session.resolve_submit_control_target = AsyncMock(  # type: ignore[method-assign]
            return_value="e10"
        )
        await self._approve(session)

        result = await session.submit_approved_application()

        self.assertIn("clicked", result)
        invoked = [call.args[0] for call in call_tool.await_args_list]
        self.assertIn("browser_click", invoked)
        self.assertIn("browser_snapshot", invoked)

    async def test_submit_approved_application_locked_without_approval(self) -> None:
        session, call_tool = self._session(is_submit=True, submit_targets={"e10"})
        session.activate_submission_guard()
        session.resolve_submit_control_target = AsyncMock(  # type: ignore[method-assign]
            return_value="e10"
        )

        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.submit_approved_application()

        call_tool.assert_not_awaited()

    async def test_non_auth_form_cannot_use_auth_submit_path(self) -> None:
        session, call_tool = self._session(is_submit=False)
        session.activate_submission_guard()

        with (
            patch(
                "z_apply_core.browser_session.resolve_auth_submit_control",
                new=AsyncMock(return_value=None),
            ),
            self.assertRaisesRegex(
                BrowserToolExecutionError,
                "structurally identifiable login",
            ),
        ):
            await session.submit_auth_form("e10")

        call_tool.assert_not_awaited()

    async def test_stale_submit_target_becomes_recoverable_browser_error(self) -> None:
        session, call_tool = self._session(is_submit=False)
        session.activate_submission_guard()
        session._classify_submit_control = AsyncMock(  # type: ignore[method-assign]
            side_effect=BrowserToolExecutionError(
                "Cannot inspect browser target 'e510'; capture a fresh snapshot and retry."
            )
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


class SubmissionGuardReArmTests(unittest.IsolatedAsyncioTestCase):
    def test_fresh_approval_rearms_after_a_consumed_submit(self) -> None:
        from z_apply_core.browser_submission import SubmissionGuard

        guard = SubmissionGuard()
        guard.approve(True)      # human approved
        guard.require_armed()    # armed
        guard.consume()          # submit clicked; approval consumed
        with self.assertRaises(ValueError):
            guard.require_armed()  # consumed -> locked
        self.assertTrue(guard.is_consumed())
        # A FRESH human approval (e.g. after a failed submission) re-arms.
        guard.approve(True)
        self.assertFalse(guard.is_consumed())
        guard.require_armed()    # no longer locked
        guard.consume()
        with self.assertRaises(ValueError):
            guard.require_armed()
        # Revoking keeps it locked: approve(False) must not re-arm.
        guard.approve(False)
        with self.assertRaises(ValueError):
            guard.require_armed()
