from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from z_apply_core.agents.duplicate_mutation_guard import DuplicateMutationGuardMiddleware
from z_apply_core.agents.orchestrator import detect_fake_tool_calls


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
        result = self._run(mw.awrap_tool_call(request, handler))
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
        result = self._run(mw.awrap_tool_call(req2, handler2))
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


class DetectFakeToolCallsTests(unittest.TestCase):
    def test_no_messages_returns_none(self) -> None:
        result = detect_fake_tool_calls([], {"messages": []})
        self.assertIsNone(result)

    def test_no_fake_patterns_returns_none(self) -> None:
        output = {"messages": [MagicMock(content="Task completed successfully.")]}
        result = detect_fake_tool_calls([], output)
        self.assertIsNone(result)

    def test_fake_pattern_with_no_executed_calls_returns_error(self) -> None:
        output = {"messages": [MagicMock(content='Now click the button with browser_click(target="e112")')]}
        result = detect_fake_tool_calls([], output)
        self.assertIsNotNone(result)
        self.assertIn("agent_protocol_error", result)
        self.assertIn("tool-call-shaped prose", result)

    def test_fake_pattern_with_executed_calls_returns_none(self) -> None:
        journal = [
            {"tool_name": "browser_click", "completed": True, "error": ""}
        ]
        output = {"messages": [MagicMock(content='Now click with browser_click(target="e112")')]}
        result = detect_fake_tool_calls(journal, output)
        self.assertIsNone(result)

    def test_multiple_fake_patterns_detected(self) -> None:
        output = {
            "messages": [
                MagicMock(
                    content='First use browser_click(target="e112") then browser_type(target="e200", text="hello")'
                )
            ]
        }
        result = detect_fake_tool_calls([], output)
        self.assertIsNotNone(result)
        self.assertIn("agent_protocol_error", result)

    def test_json_shaped_output_detected(self) -> None:
        output = {"messages": [MagicMock(content='{"text": "Upload Resume"}')]}
        result = detect_fake_tool_calls([], output)
        self.assertIsNotNone(result)
        self.assertIn("agent_protocol_error", result)


if __name__ == "__main__":
    unittest.main()
