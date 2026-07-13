from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from z_apply_core.browser_session import BrowserSession, BrowserToolExecutionError


class BrowserSubmissionGuardTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, *, is_submit: bool) -> tuple[BrowserSession, AsyncMock]:
        locator = SimpleNamespace(evaluate=AsyncMock(return_value=is_submit))
        tab = SimpleNamespace(
            resolve_target=AsyncMock(return_value=SimpleNamespace(locator=locator))
        )
        backend = SimpleNamespace(
            _ensure_tab=AsyncMock(return_value=tab),
            call_tool=AsyncMock(return_value="clicked"),
        )
        session = object.__new__(BrowserSession)
        session._backend = backend
        session.run_id = "guard-test"
        session._capture_workspace = Path("/tmp/guard-test")
        session._submission_guard_active = False
        session._approved_submissions = 0
        return session, backend.call_tool

    async def test_submit_control_is_blocked_before_browser_mutation(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session.activate_submission_guard()

        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.call_tool("browser_click", {"target": "e10"})

        call_tool.assert_not_awaited()

    async def test_approval_allows_exactly_one_successful_submit(self) -> None:
        session, call_tool = self._session(is_submit=True)
        session.activate_submission_guard()
        session.set_submit_approval(True)

        self.assertEqual(
            await session.call_tool("browser_click", {"target": "e10"}),
            "clicked",
        )
        call_tool.assert_awaited_once()

        with self.assertRaisesRegex(BrowserToolExecutionError, "submission is locked"):
            await session.call_tool("browser_click", {"target": "e10"})

    async def test_reversible_click_does_not_consume_submit_approval(self) -> None:
        session, _ = self._session(is_submit=False)
        session.activate_submission_guard()
        session.set_submit_approval(True)

        await session.call_tool("browser_click", {"target": "e5"})

        self.assertEqual(session._approved_submissions, 1)

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

        self.assertEqual(call_tool.await_count, 2)
        self.assertEqual(call_tool.await_args_list[0].args[0], "browser_click")
        self.assertEqual(call_tool.await_args_list[1].args[0], "browser_snapshot")
        self.assertIn("clicked", evidence)
        self.assertEqual(session._approved_submissions, 0)

    async def test_non_auth_form_cannot_use_auth_submit_path(self) -> None:
        session, call_tool = self._session(is_submit=False)
        session.activate_submission_guard()

        with self.assertRaisesRegex(
            BrowserToolExecutionError,
            "structurally identifiable login",
        ):
            await session.submit_auth_form("e10")

        call_tool.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
