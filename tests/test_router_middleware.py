from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, sentinel

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
)

from z_apply_core.agents.model_provider import ModelCapabilities, ModelInfo, ModelSelection
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.agents.router_middleware import (
    ModelRouter,
    build_router_middleware,
)
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.stream_events import FrameworkTraceEvent


def _make_selection(model_id: str = "agnes/agnes-2.0-flash") -> ModelSelection:
    return ModelSelection(
        info=ModelInfo(
            id=model_id,
            capabilities=ModelCapabilities(tools=True, structured=True, reasoning=True),
            metadata={"provider": "agnes"},
        ),
        llm=MagicMock(),
        callback=None,
    )


class RouterMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_orphan_tool_result_passes_through_untouched(self) -> None:
        selection = _make_selection("strict/model")
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        request = MagicMock(
            tools=[sentinel.tool],
            response_format=None,
            messages=[
                HumanMessage(content="Continue the application."),
                ToolMessage(content="stale result", tool_call_id="call-orphan"),
            ],
        )
        request.override.side_effect = lambda **values: SimpleNamespace(
            messages=request.messages, **values
        )
        handler = AsyncMock(return_value=sentinel.response)

        await ModelRouter(
            cast(Any, provider),
            role="orchestrator",
            selection=selection,
        ).awrap_model_call(request, handler)

        forwarded = handler.await_args.args[0]
        self.assertEqual(
            [type(message) for message in forwarded.messages],
            [HumanMessage, ToolMessage],
        )
        self.assertEqual(forwarded.messages[1].tool_call_id, "call-orphan")

    async def test_preserves_tool_result_with_matching_assistant_call(self) -> None:
        selection = _make_selection("strict/model")
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        request = MagicMock(
            tools=[sentinel.tool],
            response_format=None,
            messages=[
                HumanMessage(content="Continue."),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "lookup", "args": {}, "id": "call-1"}],
                ),
                ToolMessage(content="valid", tool_call_id="call-1"),
            ],
        )
        request.override.side_effect = lambda **values: SimpleNamespace(
            messages=request.messages, **values
        )
        handler = AsyncMock(return_value=sentinel.response)

        await ModelRouter(
            cast(Any, provider),
            role="orchestrator",
            selection=selection,
        ).awrap_model_call(request, handler)

        forwarded = handler.await_args.args[0]
        self.assertEqual(
            [type(message) for message in forwarded.messages],
            [HumanMessage, AIMessage, ToolMessage],
        )

    def test_factory_builds_model_router(self) -> None:
        selection = _make_selection()
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        router = build_router_middleware(
            cast(Any, provider),
            role="orchestrator",
            selection=selection,
        )
        self.assertIsInstance(router, ModelRouter)

    async def test_model_router_announces_once_and_never_rotates(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        selection = _make_selection()
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        middleware = ModelRouter(
            cast(Any, provider),
            role="orchestrator",
            selection=selection,
            sink=cast(Any, Sink()),
        )
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        handler = AsyncMock(return_value=sentinel.response)

        first = await middleware.awrap_model_call(request, handler)
        second = await middleware.awrap_model_call(request, handler)

        self.assertIs(first, sentinel.response)
        self.assertIs(second, sentinel.response)
        self.assertEqual(handler.await_count, 2)
        selected = [event for event in events if event.event == "model_selected"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].data["model_id"], "agnes/agnes-2.0-flash")
        self.assertEqual(middleware.last_model_id, "agnes/agnes-2.0-flash")

    async def test_model_router_leases_once_for_missing_selection(self) -> None:
        selection = _make_selection()
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        middleware = ModelRouter(cast(Any, provider), role="AnswerWriter")
        request = MagicMock(tools=[], response_format=None, messages=[])
        handler = AsyncMock(return_value=sentinel.response)

        await middleware.awrap_model_call(request, handler)
        await middleware.awrap_model_call(request, handler)

        provider.lease.assert_awaited_once()
        self.assertEqual(middleware.last_model_id, "agnes/agnes-2.0-flash")

    def test_model_router_reject_logs_without_rotating(self) -> None:
        selection = _make_selection()
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        middleware = ModelRouter(
            cast(Any, provider),
            role="orchestrator",
            selection=selection,
        )
        middleware.reject_active_response(
            ToolProtocolViolation("tool_protocol_failure: no progress")
        )
        self.assertEqual(middleware.last_model_id, "agnes/agnes-2.0-flash")

    async def test_model_router_emits_call_start_and_content_events(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        selection = _make_selection()
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        middleware = ModelRouter(
            cast(Any, provider),
            role="AnswerWriter",
            selection=selection,
            sink=cast(Any, Sink()),
        )
        request = MagicMock(
            tools=[sentinel.tool],
            response_format=None,
            messages=[HumanMessage(content="Fill the Skills field.")],
        )
        handler = AsyncMock(
            return_value=ModelResponse(
                result=[
                    AIMessage(
                        content="done",
                        additional_kwargs={"reasoning_content": "checked the form"},
                        tool_calls=[
                            {
                                "name": "browser_fill",
                                "args": {"target": "e1", "value": "x"},
                                "id": "c1",
                            }
                        ],
                    )
                ]
            )
        )

        await middleware.awrap_model_call(request, handler)
        await asyncio.sleep(0)  # flush the fire-and-forget content event

        started = [event for event in events if event.event == "model_call_start"]
        content = [event for event in events if event.event == "model_call_content"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(content), 1)
        self.assertEqual(started[0].data["role"], "AnswerWriter")
        self.assertEqual(started[0].data["model_id"], "agnes/agnes-2.0-flash")
        self.assertIn("Fill the Skills field", started[0].data["prompt_preview"])
        self.assertEqual(content[0].data["text"], "done")
        self.assertEqual(content[0].data["reasoning"], "checked the form")
        self.assertEqual(content[0].data["tool_calls"][0]["name"], "browser_fill")
        self.assertIn("Fill the Skills field", content[0].data["prompt_preview"])

    async def test_model_router_emits_model_failed_when_handler_raises(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        selection = _make_selection()
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        middleware = ModelRouter(
            cast(Any, provider),
            role="AnswerWriter",
            selection=selection,
            sink=cast(Any, Sink()),
        )
        request = MagicMock(tools=[], response_format=None, messages=[])
        handler = AsyncMock(side_effect=RuntimeError("boom"))

        with self.assertRaises(RuntimeError, msg="boom"):
            await middleware.awrap_model_call(request, handler)

        failed = [event for event in events if event.event == "model_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].data["role"], "AnswerWriter")
        self.assertEqual(failed[0].data["model_id"], "agnes/agnes-2.0-flash")
        self.assertEqual(failed[0].data["error_type"], "RuntimeError")
        self.assertIn("boom", failed[0].data["error"])

    async def test_model_router_appends_each_successful_call_to_ledger(self) -> None:
        ledger = RunCallLedger(job_url="https://example.test/job")
        selection = _make_selection()
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        middleware = ModelRouter(
            cast(Any, provider),
            role="orchestrator",
            selection=selection,
            ledger=ledger,
        )
        request = MagicMock(tools=[], response_format=None, messages=[])
        handler = AsyncMock(return_value=sentinel.response)

        await middleware.awrap_model_call(request, handler)
        await middleware.awrap_model_call(request, handler)

        self.assertEqual(ledger.call_count, 2)
        self.assertEqual([entry.sequence for entry in ledger.entries], [1, 2])
        self.assertTrue(all(entry.agent == "orchestrator" for entry in ledger.entries))


if __name__ == "__main__":
    unittest.main()
