from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from z_apply_core.agents.duplicate_mutation_guard import DuplicateMutationGuardMiddleware


class DuplicateMutationGuardTests(unittest.TestCase):
    def _run(self, coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_request(
        self, tool_name: str, args: dict[str, Any], call_id: str = "c1"
    ) -> MagicMock:
        request = MagicMock()
        request.tool_call = {"name": tool_name, "args": args, "id": call_id}
        return request

    def test_non_changing_tool_passes_through(self) -> None:
        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        request = self._make_request("browser_snapshot", {})
        self._run(mw.awrap_tool_call(request, handler))
        handler.assert_called_once()

    def test_first_mutation_allowed(self) -> None:
        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="clicked"))
        request = self._make_request("browser_click", {"target": "e112"})
        result = self._run(mw.awrap_tool_call(request, handler))
        handler.assert_called_once()
        self.assertEqual(result.content, "clicked")

    def test_duplicate_mutation_rejected(self) -> None:
        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="clicked"))
        req1 = self._make_request("browser_click", {"target": "e112"})
        self._run(mw.awrap_tool_call(req1, handler))

        handler2 = AsyncMock()
        req2 = self._make_request("browser_click", {"target": "e112"})
        result = self._run(mw.awrap_tool_call(req2, handler2))
        handler2.assert_not_called()
        self.assertIn("Duplicate mutation prevented", result.content)

    def test_different_args_allowed(self) -> None:
        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        req1 = self._make_request("browser_click", {"target": "e112"})
        self._run(mw.awrap_tool_call(req1, handler))
        req2 = self._make_request("browser_click", {"target": "e200"})
        self._run(mw.awrap_tool_call(req2, handler))
        self.assertEqual(handler.call_count, 2)

    def test_different_tool_allowed(self) -> None:
        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="ok"))
        req1 = self._make_request("browser_click", {"target": "e112"})
        self._run(mw.awrap_tool_call(req1, handler))
        req2 = self._make_request("browser_type", {"target": "e112", "text": "hello"})
        self._run(mw.awrap_tool_call(req2, handler))
        self.assertEqual(handler.call_count, 2)

    def test_failed_mutation_allows_retry(self) -> None:
        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="error: element not found"))
        req1 = self._make_request("browser_click", {"target": "e112"})
        self._run(mw.awrap_tool_call(req1, handler))
        handler2 = AsyncMock(return_value=MagicMock(content="clicked"))
        req2 = self._make_request("browser_click", {"target": "e112"})
        self._run(mw.awrap_tool_call(req2, handler2))
        handler2.assert_called_once()

    def test_task_resets_state(self) -> None:
        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="clicked"))
        req1 = self._make_request("browser_click", {"target": "e112"})
        self._run(mw.awrap_tool_call(req1, handler))

        task_req = self._make_request(
            "task", {"subagent_type": "BrowserSpecialist", "description": "do stuff"}
        )
        task_handler = AsyncMock(return_value=MagicMock(content="done"))
        self._run(mw.awrap_tool_call(task_req, task_handler))

        handler2 = AsyncMock(return_value=MagicMock(content="clicked again"))
        req2 = self._make_request("browser_click", {"target": "e112"})
        self._run(mw.awrap_tool_call(req2, handler2))
        handler2.assert_called_once()

    def test_non_browser_specialist_task_does_not_reset(self) -> None:
        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="clicked"))
        req1 = self._make_request("browser_click", {"target": "e112"})
        self._run(mw.awrap_tool_call(req1, handler))

        task_req = self._make_request(
            "task", {"subagent_type": "FieldMapper", "description": "map fields"}
        )
        task_handler = AsyncMock(return_value=MagicMock(content="done"))
        self._run(mw.awrap_tool_call(task_req, task_handler))

        handler2 = AsyncMock()
        req2 = self._make_request("browser_click", {"target": "e112"})
        result = self._run(mw.awrap_tool_call(req2, handler2))
        handler2.assert_not_called()
        self.assertIn("Duplicate mutation prevented", result.content)


if __name__ == "__main__":
    unittest.main()
