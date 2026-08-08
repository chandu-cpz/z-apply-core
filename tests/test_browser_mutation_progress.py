from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from z_apply_core.browser_observation import BrowserObservation
from z_apply_core.browser_session import BrowserSession, BrowserToolExecutionError


class BrowserMutationProgressTests(unittest.IsolatedAsyncioTestCase):
    def _session(self) -> tuple[BrowserSession, AsyncMock]:
        call_tool = AsyncMock(
            side_effect=["clicked", "same snapshot", "clicked elsewhere", "changed snapshot"]
        )
        backend = SimpleNamespace(
            call_tool=call_tool,
            _ensure_tab=AsyncMock(return_value=SimpleNamespace(page=MagicMock())),
        )
        session = BrowserSession(
            None,
            run_id="mutation-progress",
            backend=backend,
            tools=[],
            owns_backend=False,
        )
        session._last_snapshot = "same snapshot"
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]
        return session, call_tool

    async def test_unchanged_mutation_evidence_blocks_identical_replay(self) -> None:
        session, call_tool = self._session()

        receipt = await session.call_tool_with_inline_snapshot("browser_click", {"target": "e6"})
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

    async def test_browser_type_selects_existing_text_before_typing(self) -> None:
        select_text = AsyncMock(return_value=None)
        locator = SimpleNamespace(select_text=select_text)
        tab = SimpleNamespace(
            page=MagicMock(),
            resolve_target=AsyncMock(return_value=SimpleNamespace(locator=locator)),
        )

        async def call_tool(name: str, _arguments: object = None, **kwargs: object) -> str:
            del kwargs
            if name == "browser_snapshot":
                return "snapshot evidence"
            return "typed ok"

        backend = SimpleNamespace(
            call_tool=AsyncMock(side_effect=call_tool),
            _ensure_tab=AsyncMock(return_value=tab),
        )
        session = BrowserSession(
            None,
            run_id="type-clear",
            backend=backend,
            tools=[],
            owns_backend=False,
        )
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]

        receipt = await session.call_tool_with_inline_snapshot(
            "browser_type",
            {"target": "e90", "text": "9063812386"},
        )

        self.assertIn("typed ok", receipt)
        select_text.assert_awaited_once()
        tab.resolve_target.assert_awaited_once_with(target="e90")

    async def test_browser_type_pre_clear_failure_does_not_block_typing(self) -> None:
        tab = SimpleNamespace(
            page=MagicMock(),
            resolve_target=AsyncMock(side_effect=RuntimeError("stale ref")),
        )

        async def call_tool(name: str, _arguments: object = None, **kwargs: object) -> str:
            del kwargs
            if name == "browser_snapshot":
                return "snapshot evidence"
            return "typed ok"

        backend = SimpleNamespace(
            call_tool=AsyncMock(side_effect=call_tool),
            _ensure_tab=AsyncMock(return_value=tab),
        )
        session = BrowserSession(
            None,
            run_id="type-clear-fail",
            backend=backend,
            tools=[],
            owns_backend=False,
        )
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]

        receipt = await session.call_tool_with_inline_snapshot(
            "browser_type",
            {"target": "e90", "text": "x"},
        )

        self.assertIn("typed ok", receipt)

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
