from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import ToolMessage

from z_apply_core.agents.application_progress import ApplicationProgress
from z_apply_core.agents.specialist_contract import SpecialistCompletionContractMiddleware


class SpecialistCompletionContractTests(unittest.TestCase):
    def _run(self, coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_task_request(
        self,
        subagent_type: str = "FieldMapper",
        call_id: str = "c1",
    ) -> MagicMock:
        request = MagicMock()
        request.tool_call = {
            "name": "task",
            "args": {"subagent_type": subagent_type, "description": "Map fields"},
            "id": call_id,
        }
        return request

    # Test A1: FieldMapper calls record_field_map → contract satisfied
    def test_field_mapper_with_record_succeeds(self) -> None:
        progress = ApplicationProgress()
        progress.field_map_commits = 0
        mw = SpecialistCompletionContractMiddleware(progress)
        handler = AsyncMock(
            return_value=ToolMessage(
                content="Typed field map recorded.",
                name="task",
                tool_call_id="c1",
            )
        )

        request = self._make_task_request("FieldMapper")
        self._run(mw.awrap_tool_call(request, handler))
        handler.assert_called_once()

    # Test A2: FieldMapper finishes without record_field_map → SPECIALIST_FAILURE
    def test_field_mapper_without_record_returns_failure(self) -> None:
        progress = ApplicationProgress()
        progress.field_map_commits = 0
        mw = SpecialistCompletionContractMiddleware(progress)
        handler = AsyncMock(
            return_value=ToolMessage(
                content="I mapped the fields.",
                name="task",
                tool_call_id="c1",
            )
        )

        request = self._make_task_request("FieldMapper")
        result = self._run(mw.awrap_tool_call(request, handler))
        self.assertIsInstance(result, ToolMessage)
        self.assertIn("SPECIALIST_FAILURE", result.content)
        payload = json.loads(result.content.split("\n", 1)[1])
        self.assertEqual(payload["kind"], "required_tool_missing")
        self.assertEqual(payload["required_tool"], "record_field_map")
        self.assertFalse(payload["committed"])

    # Test A3: FieldMapper counter incremented before task → contract satisfied
    def test_field_mapper_with_incremented_counter_succeeds(self) -> None:
        progress = ApplicationProgress()
        progress.field_map_commits = 0
        mw = SpecialistCompletionContractMiddleware(progress)

        async def handler_with_increment(req: Any) -> ToolMessage:
            progress.field_map_commits += 1
            return ToolMessage(content="Done", name="task", tool_call_id="c1")

        request = self._make_task_request("FieldMapper")
        result = self._run(mw.awrap_tool_call(request, handler_with_increment))
        self.assertIsInstance(result, ToolMessage)
        self.assertNotIn("SPECIALIST_FAILURE", result.content)

    # Test A4: Non-FieldMapper specialist → passes through
    def test_non_field_mapper_passes_through(self) -> None:
        progress = ApplicationProgress()
        mw = SpecialistCompletionContractMiddleware(progress)
        handler = AsyncMock(
            return_value=ToolMessage(
                content="Done", name="task", tool_call_id="c1"
            )
        )

        request = self._make_task_request("BrowserSpecialist")
        self._run(mw.awrap_tool_call(request, handler))
        handler.assert_called_once()

    # Test A5: Non-task tool → passes through
    def test_non_task_tool_passes_through(self) -> None:
        progress = ApplicationProgress()
        mw = SpecialistCompletionContractMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))

        request = MagicMock()
        request.tool_call = {"name": "browser_click", "args": {"target": "e1"}, "id": "c1"}
        self._run(mw.awrap_tool_call(request, handler))
        handler.assert_called_once()

    # Test A6: FieldMapper task that raises → error ToolMessage
    def test_field_mapper_exception_returns_error(self) -> None:
        progress = ApplicationProgress()
        mw = SpecialistCompletionContractMiddleware(progress)
        handler = AsyncMock(side_effect=RuntimeError("browser crashed"))

        request = self._make_task_request("FieldMapper")
        result = self._run(mw.awrap_tool_call(request, handler))
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("browser crashed", result.content)


if __name__ == "__main__":
    unittest.main()
