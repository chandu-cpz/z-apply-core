from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        return session, call_tool

    async def test_unchanged_mutation_evidence_blocks_identical_replay(self) -> None:
        session, call_tool = self._session()

        self.assertEqual(
            await session.call_tool_with_inline_snapshot("browser_click", {"target": "e6"}),
            "same snapshot",
        )
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

        self.assertEqual(result, "changed snapshot")
        self.assertEqual(call_tool.await_count, 4)


if __name__ == "__main__":
    unittest.main()
