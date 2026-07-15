from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from z_apply_core.browser_observation import BrowserObservation
from z_apply_core.browser_session import BrowserSession, BrowserToolExecutionError


class BrowserMutationProgressTests(unittest.IsolatedAsyncioTestCase):
    def _session(self) -> tuple[BrowserSession, AsyncMock]:
        call_tool = AsyncMock(
            side_effect=["clicked", "same snapshot", "clicked elsewhere", "changed snapshot"]
        )
        backend = SimpleNamespace(call_tool=call_tool)
        session = object.__new__(BrowserSession)
        session._backend = backend
        session.run_id = "mutation-progress"
        session._capture_workspace = Path("/tmp/mutation-progress")
        session._submission_guard_active = False
        session._approved_submissions = 0
        session._last_snapshot = "same snapshot"
        session._last_mutation_signature = ""
        session._last_mutation_made_progress = True
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]
        return session, call_tool

    async def test_unchanged_mutation_evidence_blocks_identical_replay(self) -> None:
        session, call_tool = self._session()

        receipt = await session.call_tool_with_inline_snapshot(
            "browser_click", {"target": "e6"}
        )
        self.assertIn("BROWSER ACTION RECEIPT", receipt)
        self.assertIn("before_revision: 1", receipt)
        self.assertIn("after_revision: 1", receipt)
        self.assertIn("changed: false", receipt)
        self.assertIn("same snapshot", receipt)
        with self.assertRaisesRegex(BrowserToolExecutionError, "Duplicate mutation prevented"):
            await session.call_tool_with_inline_snapshot("browser_click", {"target": "e6"})

        self.assertEqual(call_tool.await_count, 2)

    async def test_different_mutation_is_allowed_after_unchanged_action(self) -> None:
        session, call_tool = self._session()

        await session.call_tool_with_inline_snapshot("browser_click", {"target": "e6"})
        result = await session.call_tool_with_inline_snapshot(
            "browser_click",
            {"target": "e208"},
        )

        self.assertIn("before_revision: 1", result)
        self.assertIn("after_revision: 2", result)
        self.assertIn("changed: true", result)
        self.assertIn("changed snapshot", result)
        self.assertEqual(call_tool.await_count, 4)

    async def test_bounded_wait_returns_fresh_inline_observation(self) -> None:
        session = object.__new__(BrowserSession)
        session._last_observation = None
        observation = BrowserObservation.create(
            revision=7,
            url="https://example.test/application",
            title="Application",
            evidence="- document: Sign In",
        )

        async def call_tool(name: str, _arguments: object = None) -> str:
            if name == "browser_snapshot":
                session._last_observation = observation
                return observation.evidence
            return "Waited for 2 seconds."

        session.call_tool = AsyncMock(side_effect=call_tool)  # type: ignore[method-assign]

        result = await session.call_bounded_wait("browser_wait_for", {"time": 2})

        self.assertIn("Waited for 2 seconds.", result)
        self.assertIn("BROWSER OBSERVATION", result)
        self.assertIn("revision: 7", result)
        self.assertEqual(session.call_tool.await_count, 2)


if __name__ == "__main__":
    unittest.main()
