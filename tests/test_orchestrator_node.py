from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from z_apply_core.nodes.orchestrator import _fresh_snapshot
from z_apply_core.runtime import RunRuntime


class OrchestratorNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_snapshot_failure_preserves_previous_snapshot(self) -> None:
        browser = SimpleNamespace(
            tools=SimpleNamespace(
                call=AsyncMock(side_effect=RuntimeError("browser connection closed"))
            )
        )
        runtime = RunRuntime(
            display=MagicMock(),
            live_view=MagicMock(),
            browser=browser,  # type: ignore[arg-type]
        )

        snapshot = await _fresh_snapshot(
            {"runtime": runtime, "snapshot": "last trusted browser snapshot"}  # type: ignore[typeddict-item]
        )

        self.assertEqual(snapshot, "last trusted browser snapshot")


if __name__ == "__main__":
    unittest.main()
