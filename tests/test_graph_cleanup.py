from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from z_apply_core.graph import run_job


class GraphCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_closes_runtime_created_during_setup(self) -> None:
        runtime = SimpleNamespace(close=AsyncMock())
        graph = MagicMock()

        def start_stream(*_args: object, **kwargs: object) -> object:
            config = kwargs["config"]  # type: ignore[index]
            resources = config["configurable"]["run_resources"]  # type: ignore[index]
            resources.runtime = runtime
            return object()

        graph.astream_events.side_effect = start_stream

        with (
            patch("z_apply_core.graph.build_graph", return_value=graph),
            patch("z_apply_core.graph.NimRouter"),
            patch(
                "z_apply_core.graph.consume_v3_events",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await run_job("https://example.test/job", task="Apply")

        runtime.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
