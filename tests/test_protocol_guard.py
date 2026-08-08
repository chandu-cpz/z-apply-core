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

    # Prose tool invocations are allowed: only typed runtime evidence is
    # runtime-owned, and the native tool-call channel is the only executor.
    def test_prose_tool_invocation_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content="We should now do this: task(subagent_type='BrowserSpecialist', description='Click')",
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    def test_dotted_invocation_allowed(self) -> None:
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
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    def test_json_tool_call_imitation_allowed(self) -> None:
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
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    def test_serialized_parameter_markup_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "authentication_verified"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=("authentication_verified <parameter=evidence>dashboard visible"),
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    def test_fabricated_transcript_allowed(self) -> None:
        tool = MagicMock()
        tool.name = "task"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=(
                        "task(subagent_type='FieldMapper', description='Map')\n\n"
                        "FIELD_MAPPER_RESULT: Gender, Email\n\n"
                        "ANSWER_WRITER_RESULT: Male"
                    ),
                    tool_calls=[],
                ),
            ]
        )
        middleware = ProseToolCallGuardMiddleware()
        handler = AsyncMock(return_value=response)
        self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(handler.call_count, 1)

    # Typed runtime evidence is the only violation boundary.
    def test_fabricated_observation_rejected(self) -> None:
        tool = MagicMock()
        tool.name = "browser_observe"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=(
                        "BROWSER OBSERVATION\n"
                        "url: https://example.com/jobs/1\n"
                        "form: [textbox: full_name]"
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

    def test_runtime_receipt_is_rejected_beside_native_tool_call(self) -> None:
        tool = MagicMock()
        tool.name = "browser_observe"
        request = _make_request(tools=[tool])
        invalid = _make_response(
            [
                AIMessage(
                    content=(
                        "BROWSER ACTION RECEIPT\naction: browser_click\ntarget: e420\nchanged: true"
                    ),
                    tool_calls=[{"name": "browser_observe", "args": {}, "id": "observe-1"}],
                )
            ]
        )
        valid = _make_response(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "browser_click",
                            "args": {"target": "e420"},
                            "id": "click-1",
                        }
                    ],
                )
            ]
        )
        handler = AsyncMock(side_effect=[invalid, valid])

        result = self._run(ProseToolCallGuardMiddleware().awrap_model_call(request, handler))

        self.assertEqual(handler.await_count, 2)
        self.assertEqual(result.result[0].tool_calls[0]["name"], "browser_click")

    def test_repeated_violation_raises(self) -> None:
        tool = MagicMock()
        tool.name = "browser_observe"
        request = _make_request(tools=[tool])
        response = _make_response(
            [
                AIMessage(
                    content=(
                        "BROWSER OBSERVATION\n"
                        "url: https://example.com/jobs/1\n"
                        "form: [textbox: full_name]"
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

    # Normal prose and mixed native calls are allowed.
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

    # No tools still validates: runtime evidence is independent of tool scope.
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

    # Correction is a HumanMessage; retry succeeds.
    def test_correction_uses_human_message(self) -> None:
        tool = MagicMock()
        tool.name = "browser_observe"
        request = _make_request(tools=[tool], messages=[MagicMock()])
        call_count = 0

        async def handler(req: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(
                    [
                        AIMessage(
                            content="BROWSER OBSERVATION\nurl: https://example.com/jobs/1",
                            tool_calls=[],
                        ),
                    ]
                )
            return _make_response(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "browser_observe",
                                "args": {},
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

    def test_successful_correction_on_retry(self) -> None:
        tool = MagicMock()
        tool.name = "browser_observe"
        request = _make_request(tools=[tool])
        call_count = 0

        async def handler(req: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(
                    [
                        AIMessage(
                            content="BROWSER ACTION RECEIPT\naction: browser_click\ntarget: e420",
                            tool_calls=[],
                        ),
                    ]
                )
            return _make_response(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "browser_click",
                                "args": {"target": "e420"},
                                "id": "c2",
                            }
                        ],
                    ),
                ]
            )

        middleware = ProseToolCallGuardMiddleware()
        result = self._run(middleware.awrap_model_call(request, handler))
        self.assertEqual(call_count, 2)
        self.assertEqual(result.result[0].tool_calls[0]["name"], "browser_click")


if __name__ == "__main__":
    unittest.main()
