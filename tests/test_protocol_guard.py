from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

from z_apply_core.agents.protocol_guard import (
    ProseToolCallGuardMiddleware,
    ToolProtocolViolation,
)


def _make_request(
    tools: list[Any] | None = None,
    messages: list[Any] | None = None,
) -> MagicMock:
    request = MagicMock()
    request.tools = tools or []
    request.messages = messages or []
    return request


def _make_response(messages: list[AIMessage]) -> MagicMock:
    result = MagicMock()
    result.result = messages
    return result


class ProseToolCallGuardTests(unittest.TestCase):
    def _run(self, coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    # Test A: Exact production failure — tool_calls present AND fabricated prose
    def test_exact_production_failure_with_fabricated_prose(self) -> None:
        tool = MagicMock()
        tool.name = "write_todos"
        request = _make_request(tools=[tool])
        response = _make_response([
            AIMessage(
                content=(
                    "I'll start by dispatching the task.\n\n"
                    "task(subagent_type='FieldMapper', description='Map fields')\n\n"
                    "FIELD_MAPPER_RESULT: Gender, Email, Phone..."
                ),
                tool_calls=[{"name": "write_todos", "args": {"todos": ["Step 1"]}, "id": "c1"}],
            ),
        ])
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))

    # Test B: Successful correction — first prose, retry native
    def test_successful_correction_on_retry(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        first_response = _make_response([
            AIMessage(
                content="task(subagent_type='BrowserSpecialist', description='Click submit')",
                tool_calls=[],
            ),
        ])
        corrected_response = _make_response([
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "task",
                    "args": {
                        "subagent_type": "BrowserSpecialist",
                        "description": "Click submit",
                    },
                    "id": "c2",
                }],
            ),
        ])
        call_count = 0

        async def handler(req: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_response
            return corrected_response

        middleware = ProseToolCallGuardMiddleware()
        result = self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(call_count, 2)
        self.assertEqual(result.result[0].tool_calls[0]["name"], "task")

    # Test C: Repeated violation — both attempts fail
    def test_repeated_violation_raises(self) -> None:
        tool = MagicMock()
        tool.name = "write_todos"
        request = _make_request(tools=[tool])
        response = _make_response([
            AIMessage(
                content="write_todos(todos=['Step 1'])",
                tool_calls=[],
            ),
        ])
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 2)

    # Test D: Mixed legitimate prose + native tool call
    def test_mixed_prose_and_native_tool_call_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response([
            AIMessage(
                content="I'll inspect the form now.",
                tool_calls=[{
                    "name": "task",
                    "args": {
                        "subagent_type": "BrowserSpecialist",
                        "description": "Inspect form",
                    },
                    "id": "c3",
                }],
            ),
        ])
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        result = self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(result.result[0].tool_calls[0]["name"], "task")

    # Test E: Normal prose containing "task" — not an invocation
    def test_normal_prose_with_task_word_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "write_todos"
        request = _make_request(tools=[tool])
        response = _make_response([
            AIMessage(
                content="The task has been completed successfully. All fields are mapped.",
                tool_calls=[],
            ),
        ])
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    # Test F: Native task RESUME_PATH replacement
    def test_native_task_resume_path_replaced(self) -> None:
        from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware

        middleware = SubagentDispatchMiddleware(
            ["BrowserSpecialist"],
            resume_path="/actual/resume.pdf",
        )
        message = AIMessage(
            content="",
            tool_calls=[{
                "name": "task",
                "args": {
                    "subagent_type": "BrowserSpecialist",
                    "description": "Upload RESUME_PATH to the form",
                },
                "id": "call-1",
            }],
        )
        result = middleware._normalize_message(message)
        desc = result.tool_calls[0]["args"]["description"]
        self.assertIn("/actual/resume.pdf", desc)
        self.assertNotIn("RESUME_PATH", desc)

    # Test G2: Standalone *_RESULT: without preceding fake call
    def test_standalone_result_marker_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response([
            AIMessage(
                content=(
                    "Based on the FIELD_MAPPER_RESULT, the Gender field is required.\n"
                    "I will now dispatch a task."
                ),
                tool_calls=[{
                    "name": "task",
                    "args": {
                        "subagent_type": "FieldMapper",
                        "description": "Map",
                    },
                    "id": "c4",
                }],
            ),
        ])
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    # Test H: No tools — skip validation
    def test_no_tools_skips_validation(self) -> None:
        request = _make_request(tools=[])
        response = _make_response([
            AIMessage(
                content="task(subagent_type='BrowserSpecialist', description='Click')",
                tool_calls=[],
            ),
        ])
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    # Test I: Duplicate mutation survives snapshot (tested via DuplicateMutationGuard)
    def test_duplicate_mutation_survives_snapshot(self) -> None:
        from z_apply_core.agents.duplicate_mutation_guard import DuplicateMutationGuardMiddleware

        mw = DuplicateMutationGuardMiddleware()
        handler = AsyncMock(return_value=MagicMock(content="clicked"))

        req1 = MagicMock()
        req1.tool_call = {"name": "browser_click", "args": {"target": "e112"}, "id": "c1"}
        self._run(mw.awrap_tool_call(req1, handler))

        req_snapshot = MagicMock()
        req_snapshot.tool_call = {"name": "browser_snapshot", "args": {}, "id": "c2"}
        self._run(mw.awrap_tool_call(req_snapshot, handler))

        req2 = MagicMock()
        req2.tool_call = {"name": "browser_click", "args": {"target": "e112"}, "id": "c3"}
        result = self._run(mw.awrap_tool_call(req2, AsyncMock()))
        self.assertIn("Duplicate mutation prevented", result.content)


if __name__ == "__main__":
    unittest.main()
