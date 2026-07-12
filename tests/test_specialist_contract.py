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

    def _parse_failure(self, result: ToolMessage) -> dict[str, Any]:
        return json.loads(result.content.split("\n", 1)[1])

    # Test A1: FieldMapper calls record_field_map → contract satisfied
    def test_field_mapper_with_record_succeeds(self) -> None:
        progress = ApplicationProgress()
        progress.field_map_commits = 0
        mw = SpecialistCompletionContractMiddleware(progress)

        async def handler_with_increment(req: Any) -> ToolMessage:
            progress.field_map_commits += 1
            return ToolMessage(
                content="Typed field map recorded.",
                name="task",
                tool_call_id="c1",
            )

        request = self._make_task_request("FieldMapper")
        result = self._run(mw.awrap_tool_call(request, handler_with_increment))
        self.assertIsInstance(result, ToolMessage)
        self.assertNotIn("SPECIALIST_FAILURE", result.content)

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
        payload = self._parse_failure(result)
        self.assertEqual(payload["kind"], "required_tool_missing")
        self.assertEqual(payload["required_tool"], "record_field_map")
        self.assertFalse(payload["committed"])
        self.assertEqual(payload["role"], "FieldMapper")
        self.assertEqual(payload["recovery_owner"], "Orchestrator")

    # Test A3: Non-FieldMapper specialist → passes through
    def test_non_field_mapper_passes_through(self) -> None:
        progress = ApplicationProgress()
        mw = SpecialistCompletionContractMiddleware(progress)
        handler = AsyncMock(
            return_value=ToolMessage(
                content="Done", name="task", tool_call_id="c1",
            )
        )

        request = self._make_task_request("BrowserSpecialist")
        self._run(mw.awrap_tool_call(request, handler))
        handler.assert_called_once()

    # Test A4: Non-task tool → passes through
    def test_non_task_tool_passes_through(self) -> None:
        progress = ApplicationProgress()
        mw = SpecialistCompletionContractMiddleware(progress)
        handler = AsyncMock(return_value=MagicMock(content="ok"))

        request = MagicMock()
        request.tool_call = {
            "name": "browser_click",
            "args": {"target": "e1"},
            "id": "c1",
        }
        self._run(mw.awrap_tool_call(request, handler))
        handler.assert_called_once()

    # Test A5: FieldMapper exception WITHOUT prior commit → SPECIALIST_FAILURE
    def test_field_mapper_exception_without_commit_returns_failure(self) -> None:
        progress = ApplicationProgress()
        mw = SpecialistCompletionContractMiddleware(progress)
        handler = AsyncMock(side_effect=RuntimeError("browser crashed"))

        request = self._make_task_request("FieldMapper")
        result = self._run(mw.awrap_tool_call(request, handler))
        self.assertIsInstance(result, ToolMessage)
        self.assertIn("SPECIALIST_FAILURE", result.content)
        payload = self._parse_failure(result)
        self.assertEqual(payload["kind"], "specialist_exception")
        self.assertFalse(payload["committed"])
        self.assertIn("browser crashed", payload["detail"])

    # Test A6: FieldMapper exception WITH prior commit → preserve committed state
    def test_field_mapper_exception_with_prior_commit_preserves_state(self) -> None:
        progress = ApplicationProgress()
        progress.field_map_commits = 0
        mw = SpecialistCompletionContractMiddleware(progress)

        async def handler_crash_after_commit(req: Any) -> ToolMessage:
            progress.field_map_commits += 1
            raise RuntimeError("crashed after record_field_map")

        request = self._make_task_request("FieldMapper")
        result = self._run(mw.awrap_tool_call(request, handler_crash_after_commit))
        self.assertIsInstance(result, ToolMessage)
        self.assertIn("SPECIALIST_FAILURE", result.content)
        payload = self._parse_failure(result)
        self.assertEqual(payload["kind"], "specialist_exception_after_commit")
        self.assertTrue(payload["committed"])
        self.assertIn("crashed after record_field_map", payload["detail"])

    # Test A7: Prose-only FieldMapper result is NOT treated as committed
    def test_prose_only_result_not_treated_as_committed(self) -> None:
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
        result = self._run(mw.awrap_tool_call(request, handler))
        self.assertIn("SPECIALIST_FAILURE", result.content)
        payload = self._parse_failure(result)
        self.assertEqual(payload["kind"], "required_tool_missing")


if __name__ == "__main__":
    unittest.main()
