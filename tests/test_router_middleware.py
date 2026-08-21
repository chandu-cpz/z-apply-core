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
)

from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.agents.router_middleware import (
    ModelRouter,
    build_router_middleware,
)
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.stream_events import FrameworkTraceEvent


class FakeGateway:
    """ModelGateway stand-in: get_model returns a fresh client per generation.

    Bump ``generation`` to simulate a live provider/model switch: the gateway
    cache is invalidated and the next get_model returns a different object.
    """

    def __init__(self, name: str = "agnes", model_id: str = "agnes/agnes-2.0-flash") -> None:
        self.name = name
        self.model_id = model_id
        self.generation = 0
        self._client: Any = None
        self._client_generation = -1

    def get_model(self, thinking_effort: str | None = None) -> Any:
        # Mirrors the real gateway: same object until the config changes.
        if self._client_generation != self.generation:
            self._client = MagicMock(model=f"{self.model_id}#{self.generation}")
            self._client_generation = self.generation
        return self._client


class RouterMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_orphan_tool_result_passes_through_untouched(self) -> None:
        gateway = FakeGateway("strict", "strict/model")
        request = MagicMock(
            tools=[sentinel.tool],
            response_format=None,
            messages=[
                HumanMessage(content="Continue the application."),
            ],
        )
        request.override.side_effect = lambda **values: SimpleNamespace(
            messages=request.messages, **values
        )
        handler = AsyncMock(return_value=sentinel.response)

        await ModelRouter(cast(Any, gateway), role="orchestrator").awrap_model_call(
            request, handler
        )

        forwarded = handler.await_args.args[0]
        self.assertEqual([type(m) for m in forwarded.messages], [HumanMessage])

    def test_factory_builds_model_router(self) -> None:
        router = build_router_middleware(cast(Any, FakeGateway()), role="orchestrator")
        self.assertIsInstance(router, ModelRouter)

    async def test_model_router_announces_once_and_reuses_client(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        gateway = FakeGateway()
        middleware = ModelRouter(
            cast(Any, gateway),
            role="orchestrator",
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
        # Same client object drove both requests (gateway cache identity).
        first_model = handler.await_args_list[0].args[0].model
        second_model = handler.await_args_list[1].args[0].model
        self.assertIs(first_model, second_model)

    async def test_live_switch_swaps_client_and_reannounces(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        gateway = FakeGateway()
        middleware = ModelRouter(
            cast(Any, gateway),
            role="orchestrator",
            sink=cast(Any, Sink()),
        )
        request = MagicMock(tools=[], response_format=None, messages=[])
        request.override.side_effect = lambda **values: SimpleNamespace(
            messages=request.messages, **values
        )
        handler = AsyncMock(return_value=sentinel.response)

        await middleware.awrap_model_call(request, handler)
        gateway.model_id = "groq/qwen3.6-27b"
        gateway.generation = 1  # cache invalidated: next client is a new object
        await middleware.awrap_model_call(request, handler)
        await middleware.awrap_model_call(request, handler)

        selected = [event for event in events if event.event == "model_selected"]
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[1].data["model_id"], "groq/qwen3.6-27b")
        # The new client drives requests from the switch onward.
        first_model = handler.await_args_list[0].args[0].model
        second_model = handler.await_args_list[1].args[0].model
        third_model = handler.await_args_list[2].args[0].model
        self.assertIsNot(second_model, first_model)
        self.assertIs(second_model, third_model)

    async def test_per_role_effort_is_passed_to_the_gateway(self) -> None:
        seen: list[str | None] = []

        class EffortGateway(FakeGateway):
            def get_model(self, thinking_effort: str | None = None) -> Any:
                seen.append(thinking_effort)
                return MagicMock(model="m")

        middleware = ModelRouter(cast(Any, EffortGateway()), role="AuthenticationSpecialist")
        request = MagicMock(tools=[], response_format=None, messages=[])
        handler = AsyncMock(return_value=sentinel.response)
        await middleware.awrap_model_call(request, handler)
        self.assertEqual(seen, ["low"])

    def test_model_router_reject_logs_without_rotating(self) -> None:
        middleware = ModelRouter(cast(Any, FakeGateway()), role="orchestrator")
        middleware.reject_active_response(
            ToolProtocolViolation("tool_protocol_failure: no progress")
        )
        self.assertEqual(middleware.last_model_id, "agnes/agnes-2.0-flash")

    async def test_model_router_emits_call_start_and_content_events(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        middleware = ModelRouter(
            cast(Any, FakeGateway()),
            role="AnswerWriter",
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
        self.assertEqual(started[0].data["provider"], "agnes")
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

        middleware = ModelRouter(
            cast(Any, FakeGateway()),
            role="AnswerWriter",
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
        middleware = ModelRouter(
            cast(Any, FakeGateway()),
            role="orchestrator",
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

