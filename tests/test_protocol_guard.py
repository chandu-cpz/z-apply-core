from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage, HumanMessage

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

    # Test D: Mixed native + fabricated prose
    def test_mixed_native_and_fabricated_prose(self) -> None:
        tool = MagicMock()
        tool.name = "write_todos"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=(
                        "I'll start by dispatching the task.\n\n"
                        "task(subagent_type='FieldMapper', description='Map fields')\n\n"
                        "FIELD_MAPPER_RESULT: Gender, Email, Phone..."
                    ),
                    tool_calls=[{"name": "write_todos", "args": {"todos": ["Step 1"]}, "id": "c1"}],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))

    # Test E: JSON tool-call imitation
    def test_json_tool_call_imitation(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        payload = json.dumps(
            {
                "tool": "task",
                "params": {"subagent_type": "BrowserSpecialist"},
            }
        )
        response = _make_response(
            [
                AIMessage(content=payload, tool_calls=[]),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))

    def test_provider_parameter_markup_is_not_treated_as_a_tool_call(self) -> None:
        tool = MagicMock()
        tool.name = "authentication_verified"
        request = _make_request(tools=[tool])
        invalid = _make_response(
            [
                AIMessage(
                    content=("authentication_verified <parameter=evidence>dashboard visible"),
                    tool_calls=[],
                ),
            ]
        )
        valid = _make_response(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "authentication_verified",
                            "args": {"evidence": "dashboard visible"},
                            "id": "call-2",
                        }
                    ],
                ),
            ]
        )
        handler = AsyncMock(side_effect=[invalid, valid])

        result = self._run(ProseToolCallGuardMiddleware().awrap_model_call(request, handler))

        self.assertEqual(result.result[0].tool_calls[0]["name"], "authentication_verified")
        self.assertEqual(handler.await_count, 2)

    def test_json_name_arguments_imitation(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=json.dumps(
                        {"name": "task", "arguments": {"subagent_type": "FieldMapper"}}
                    ),
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))

    # Test F: Inline invocation (not at line start)
    def test_inline_invocation_caught(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        content = (
            "We should now do this: task(subagent_type='BrowserSpecialist', description='Click')"
        )
        response = _make_response(
            [
                AIMessage(content=content, tool_calls=[]),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))

    def test_dotted_invocation_caught(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content="Let me call tool.task(subagent_type='FieldMapper') now.",
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))

    # Test G: Normal prose allowed
    def test_normal_prose_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "write_todos"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content="The task has been completed successfully. All fields are mapped.",
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    def test_no_parentheses_after_tool_name_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content="I will dispatch a task for the BrowserSpecialist.",
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    # Test H: Correction uses HumanMessage
    def test_correction_uses_human_message(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool], messages=[MagicMock()])
        call_count = 0

        async def handler(req: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                content = "task(subagent_type='BrowserSpecialist', description='Click')"
                return _make_response(
                    [
                        AIMessage(content=content, tool_calls=[]),
                    ]
                )
            return _make_response(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {
                                    "subagent_type": "BrowserSpecialist",
                                    "description": "Click",
                                },
                                "id": "c2",
                            }
                        ],
                    ),
                ]
            )

        middleware = ProseToolCallGuardMiddleware()
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(call_count, 2)
        retry_call = request.override.call_args
        retry_messages = retry_call.kwargs.get("messages", retry_call[1].get("messages", []))
        correction_msg = retry_messages[-1]
        self.assertIsInstance(correction_msg, HumanMessage)
        self.assertIn("RUNTIME PROTOCOL ERROR", correction_msg.content)

    # Test: Successful correction on retry
    def test_successful_correction_on_retry(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        call_count = 0

        async def handler(req: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                content = "task(subagent_type='BrowserSpecialist', description='Click submit')"
                return _make_response(
                    [
                        AIMessage(content=content, tool_calls=[]),
                    ]
                )
            return _make_response(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {
                                    "subagent_type": "BrowserSpecialist",
                                    "description": "Click submit",
                                },
                                "id": "c2",
                            }
                        ],
                    ),
                ]
            )

        middleware = ProseToolCallGuardMiddleware()
        result = self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(call_count, 2)
        self.assertEqual(result.result[0].tool_calls[0]["name"], "task")

    # Test: Repeated violation raises
    def test_repeated_violation_raises(self) -> None:
        tool = MagicMock()
        tool.name = "write_todos"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content="write_todos(todos=['Step 1'])",
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 2)

    # Test: No tools skips validation
    def test_no_tools_skips_validation(self) -> None:
        request = _make_request(tools=[])
        response = _make_response(
            [
                AIMessage(
                    content="task(subagent_type='BrowserSpecialist', description='Click')",
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    # Test: Fabricated transcript bundle — prose calls + RESULT markers
    def test_fabricated_transcript_bundle(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=(
                        "task(subagent_type='FieldMapper', description='Map')\n\n"
                        "FIELD_MAPPER_RESULT: Gender, Email\n\n"
                        "task(subagent_type='AnswerWriter', description='Answer')\n\n"
                        "ANSWER_WRITER_RESULT: Male"
                    ),
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))

    # Test: Fabricated transcript detector fires when prose calls present
    def test_fabricated_transcript_detector_fires_with_prose_calls(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=(
                        "task(subagent_type='FieldMapper', description='Map')\n\n"
                        "FIELD_MAPPER_RESULT: Gender"
                    ),
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        with self.assertRaises(ToolProtocolViolation):
            self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 2)

    # Test: Standalone RESULT marker without prose calls → allowed
    def test_standalone_result_marker_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=(
                        "Based on the FIELD_MAPPER_RESULT, the Gender field is required.\n"
                        "I will now dispatch a task."
                    ),
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "subagent_type": "FieldMapper",
                                "description": "Map",
                            },
                            "id": "c4",
                        }
                    ],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    # Test: Mixed prose and native tool call allowed
    def test_mixed_prose_and_native_tool_call_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content="I'll inspect the form now.",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "subagent_type": "BrowserSpecialist",
                                "description": "Inspect form",
                            },
                            "id": "c3",
                        }
                    ],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        result = self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(result.result[0].tool_calls[0]["name"], "task")


if __name__ == "__main__":
    unittest.main()
