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
    SystemMessage,
    ToolMessage,
)
from nim_router import NimRouter
from nim_router.errors import ErrorKind
from nim_router.schemas import ModelSelection

from z_apply_core.agents.model_provider import NIMProvider, OpenCodeGoProvider
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.agents.router_middleware import (
    ORCHESTRATOR_EXCLUDED_MODEL_IDS,
    NimRouterMiddleware,
    StaticModelRouter,
    _stamp_prompt_cache_markers,
    build_router_middleware,
)
from z_apply_core.stream_events import FrameworkTraceEvent


class RouterMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_removes_orphan_tool_result_before_model_handoff(self) -> None:
        router = MagicMock(spec=NimRouter)
        model = MagicMock()
        selection = cast(
            Any,
            SimpleNamespace(info=SimpleNamespace(id="strict/model"), llm=model),
        )
        request = MagicMock(
            tools=[sentinel.tool],
            response_format=None,
            messages=[
                HumanMessage(content="Continue the application."),
                ToolMessage(content="stale result", tool_call_id="call-orphan"),
            ],
        )
        request.override.side_effect = lambda **values: SimpleNamespace(**values)
        handler = AsyncMock(return_value=sentinel.response)

        await NimRouterMiddleware(
            router,
            role="orchestrator",
            initial_selection=selection,
        ).awrap_model_call(request, handler)

        forwarded = handler.await_args.args[0]
        self.assertEqual([type(message) for message in forwarded.messages], [HumanMessage])

    async def test_preserves_tool_result_with_matching_assistant_call(self) -> None:
        router = MagicMock(spec=NimRouter)
        model = MagicMock()
        selection = cast(
            Any,
            SimpleNamespace(info=SimpleNamespace(id="strict/model"), llm=model),
        )
        assistant_call = AIMessage(
            content="",
            tool_calls=[{"name": "lookup", "args": {}, "id": "call-valid"}],
        )
        request = MagicMock(
            tools=[sentinel.tool],
            response_format=None,
            messages=[assistant_call, ToolMessage(content="fact", tool_call_id="call-valid")],
        )
        request.override.side_effect = lambda **values: SimpleNamespace(**values)
        handler = AsyncMock(return_value=sentinel.response)

        await NimRouterMiddleware(
            router,
            role="orchestrator",
            initial_selection=selection,
        ).awrap_model_call(request, handler)

        request.override.assert_called_once_with(model=model)

    async def test_removes_stale_replayed_tool_result_after_new_turn(self) -> None:
        router = MagicMock(spec=NimRouter)
        model = MagicMock()
        selection = cast(
            Any,
            SimpleNamespace(info=SimpleNamespace(id="strict/model"), llm=model),
        )
        assistant_call = AIMessage(
            content="",
            tool_calls=[{"name": "lookup", "args": {}, "id": "call-valid"}],
        )
        request = MagicMock(
            tools=[sentinel.tool],
            response_format=None,
            messages=[
                assistant_call,
                ToolMessage(content="fact", tool_call_id="call-valid"),
                HumanMessage(content="Continue with the form."),
                ToolMessage(content="replayed fact", tool_call_id="call-valid"),
            ],
        )
        request.override.side_effect = lambda **values: SimpleNamespace(**values)
        handler = AsyncMock(return_value=sentinel.response)

        await NimRouterMiddleware(
            router,
            role="orchestrator",
            initial_selection=selection,
        ).awrap_model_call(request, handler)

        forwarded = handler.await_args.args[0]
        self.assertEqual(
            [type(message) for message in forwarded.messages],
            [AIMessage, ToolMessage, HumanMessage],
        )

    async def test_rejects_empty_response_without_reasoning_or_tool_calls(self) -> None:
        router = MagicMock(spec=NimRouter)
        model = MagicMock()
        router.lease = AsyncMock(
            return_value=cast(
                Any,
                SimpleNamespace(info=SimpleNamespace(id="empty/model"), llm=model),
            )
        )
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        request.override.return_value = sentinel.overridden_request
        handler = AsyncMock(
            return_value=ModelResponse(result=[AIMessage(content="", tool_calls=[])])
        )

        with self.assertRaises(ToolProtocolViolation):
            await NimRouterMiddleware(router, role="AuthenticationSpecialist").awrap_model_call(
                request, handler
            )

        router.record_failure.assert_called_once()
        router.cooldown_model.assert_called_once_with("empty/model", 20.0)

    async def test_rejects_reasoning_without_a_final_answer(self) -> None:
        router = MagicMock(spec=NimRouter)
        model = MagicMock()
        router.lease = AsyncMock(
            return_value=cast(
                Any,
                SimpleNamespace(info=SimpleNamespace(id="step/model"), llm=model),
            )
        )
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        request.override.return_value = sentinel.overridden_request
        handler = AsyncMock(
            return_value=ModelResponse(
                result=[
                    AIMessage(
                        content="",
                        additional_kwargs={"reasoning_content": "Form is open."},
                    )
                ]
            )
        )

        with self.assertRaises(ToolProtocolViolation):
            await NimRouterMiddleware(router, role="AnswerWriter").awrap_model_call(
                request, handler
            )

        router.record_failure.assert_called_once()
        router.cooldown_model.assert_called_once_with("step/model", 20.0)

    async def test_keeps_inline_think_tags_and_accepts_final_answer(self) -> None:
        router = MagicMock(spec=NimRouter)
        model = MagicMock()
        selection = cast(
            Any,
            SimpleNamespace(info=SimpleNamespace(id="step/model"), llm=model),
        )
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        request.override.return_value = sentinel.overridden_request
        handler = AsyncMock(
            return_value=ModelResponse(
                result=[
                    AIMessage(
                        content=("<think>Memory lookup was exact.</think>\nGender = Male"),
                        additional_kwargs={"reasoning_content": "Memory lookup was exact."},
                    )
                ]
            )
        )

        result = await NimRouterMiddleware(
            router,
            role="AnswerWriter",
            initial_selection=selection,
        ).awrap_model_call(request, handler)

        self.assertEqual(
            result.result[0].content, "<think>Memory lookup was exact.</think>\nGender = Male"
        )

    async def test_executes_initial_exploration_selection_before_leasing_again(self) -> None:
        router = MagicMock(spec=NimRouter)
        router.lease = AsyncMock()
        initial_model = MagicMock()
        initial = cast(
            ModelSelection,
            SimpleNamespace(
                info=SimpleNamespace(id="exploration/model"),
                llm=initial_model,
            ),
        )
        request = MagicMock()
        request.tools = [sentinel.tool]
        request.response_format = None
        request.messages = []
        request.override.return_value = sentinel.overridden_request
        handler = AsyncMock(return_value=sentinel.response)
        middleware = NimRouterMiddleware(
            router,
            role="orchestrator",
            initial_selection=initial,
        )

        result = await middleware.awrap_model_call(request, handler)

        self.assertIs(result, sentinel.response)
        router.lease.assert_not_awaited()
        request.override.assert_called_once_with(model=initial_model)
        handler.assert_awaited_once_with(sentinel.overridden_request)
        router.record_success.assert_not_called()

    async def test_reuses_one_healthy_lease_across_model_turns(self) -> None:
        router = MagicMock(spec=NimRouter)
        leased_model = MagicMock()
        router.lease = AsyncMock(
            return_value=cast(
                Any,
                SimpleNamespace(
                    info=SimpleNamespace(id="next/model"),
                    llm=leased_model,
                ),
            )
        )
        request = MagicMock()
        request.tools = [sentinel.tool]
        request.response_format = None
        request.messages = []
        request.override.return_value = sentinel.overridden_request
        handler = AsyncMock(return_value=sentinel.response)
        middleware = NimRouterMiddleware(router, role="orchestrator")

        await middleware.awrap_model_call(request, handler)
        await middleware.awrap_model_call(request, handler)

        router.lease.assert_awaited_once_with(
            tools=True,
            structured=False,
            vision=False,
            reasoning=True,
            priority="balanced",
            excluded_model_ids=ORCHESTRATOR_EXCLUDED_MODEL_IDS,
        )
        self.assertEqual(request.override.call_count, 2)

    async def test_bounded_specialists_keep_full_model_pool(self) -> None:
        router = MagicMock(spec=NimRouter)
        router.lease = AsyncMock(
            return_value=cast(
                Any,
                SimpleNamespace(
                    info=SimpleNamespace(id="openai/gpt-oss-120b"),
                    llm=MagicMock(),
                ),
            )
        )
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        request.override.return_value = sentinel.overridden_request

        await NimRouterMiddleware(router, role="AnswerWriter").awrap_model_call(
            request,
            AsyncMock(return_value=sentinel.response),
        )

        router.lease.assert_awaited_once_with(
            tools=True,
            structured=False,
            vision=False,
            reasoning=True,
            priority="quality",
            excluded_model_ids=None,
        )

    async def test_authentication_specialist_excludes_unreliable_stateful_models(
        self,
    ) -> None:
        router = MagicMock(spec=NimRouter)
        router.lease = AsyncMock(
            return_value=cast(
                Any,
                SimpleNamespace(
                    info=SimpleNamespace(id="stepfun-ai/step-3.7-flash"),
                    llm=MagicMock(),
                ),
            )
        )
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        request.override.return_value = sentinel.overridden_request

        await NimRouterMiddleware(router, role="AuthenticationSpecialist").awrap_model_call(
            request,
            AsyncMock(return_value=sentinel.response),
        )

        router.lease.assert_awaited_once_with(
            tools=True,
            structured=False,
            vision=False,
            reasoning=True,
            priority="balanced",
            excluded_model_ids=ORCHESTRATOR_EXCLUDED_MODEL_IDS,
        )

    async def test_releases_failed_lease_before_retry(self) -> None:
        router = MagicMock(spec=NimRouter)
        first = SimpleNamespace(info=SimpleNamespace(id="first/model"), llm=MagicMock())
        second = SimpleNamespace(info=SimpleNamespace(id="second/model"), llm=MagicMock())
        router.lease = AsyncMock(side_effect=[first, second])
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        request.override.return_value = sentinel.overridden_request
        middleware = NimRouterMiddleware(router, role="orchestrator")

        with self.assertRaises(RuntimeError):
            await middleware.awrap_model_call(
                request,
                AsyncMock(side_effect=RuntimeError("rate limited")),
            )
        await middleware.awrap_model_call(
            request,
            AsyncMock(return_value=sentinel.response),
        )

        self.assertEqual(router.lease.await_count, 2)
        router.record_failure.assert_not_called()

    async def test_nim_router_emits_call_start_and_content_events(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        router = MagicMock(spec=NimRouter)
        selection = cast(
            Any,
            SimpleNamespace(
                info=SimpleNamespace(id="nim/model", metadata={"provider": "nim"}),
                llm=MagicMock(),
            ),
        )
        request = MagicMock(
            tools=[sentinel.tool],
            response_format=None,
            messages=[HumanMessage(content="Resolve the phone field.")],
        )
        request.override.return_value = sentinel.overridden_request
        handler = AsyncMock(return_value=ModelResponse(result=[AIMessage(content="phone set")]))

        await NimRouterMiddleware(
            router,
            role="orchestrator",
            initial_selection=selection,
            sink=Sink(),
        ).awrap_model_call(request, handler)
        await asyncio.sleep(0)  # flush the fire-and-forget content event

        started = [event for event in events if event.event == "model_call_start"]
        content = [event for event in events if event.event == "model_call_content"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(content), 1)
        self.assertEqual(started[0].data["provider"], "nim")
        self.assertEqual(started[0].data["role"], "orchestrator")
        self.assertEqual(content[0].data["text"], "phone set")
        self.assertEqual(content[0].data["tool_calls"], [])

    async def test_model_call_uses_router_wall_clock_timeout(self) -> None:
        router = MagicMock(spec=NimRouter)
        router.config = SimpleNamespace(timeout_seconds=0.01)
        selection = SimpleNamespace(
            info=SimpleNamespace(id="slow/model"),
            llm=MagicMock(),
        )
        router.lease = AsyncMock(return_value=selection)
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        request.override.return_value = sentinel.overridden_request

        async def blocked(_request: object) -> None:
            await asyncio.Event().wait()

        with self.assertRaises(TimeoutError):
            await NimRouterMiddleware(NIMProvider(router), role="AnswerWriter").awrap_model_call(
                request,
                blocked,
            )

        router.record_failure.assert_called_once()
        self.assertEqual(router.record_failure.call_args.kwargs["kind"], ErrorKind.TIMEOUT)


class StaticRouterTests(unittest.IsolatedAsyncioTestCase):
    def _selection(self, model_id: str = "agnes/agnes-2.0-flash") -> Any:
        return cast(
            Any,
            SimpleNamespace(info=SimpleNamespace(id=model_id), llm=MagicMock()),
        )

    def test_factory_uses_nim_router_only_for_nim_provider(self) -> None:
        router = MagicMock(spec=NimRouter)
        nim = build_router_middleware(
            NIMProvider(router),
            role="orchestrator",
            selection=self._selection(),
        )
        self.assertIsInstance(nim, NimRouterMiddleware)
        plain = build_router_middleware(
            SimpleNamespace(),
            role="orchestrator",
            selection=self._selection(),
        )
        self.assertIsInstance(plain, StaticModelRouter)

    def test_static_router_announces_once_and_never_rotates(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        middleware = StaticModelRouter(
            SimpleNamespace(),
            role="orchestrator",
            selection=self._selection(),
            sink=Sink(),
        )
        request = MagicMock(tools=[sentinel.tool], response_format=None, messages=[])
        handler = AsyncMock(return_value=sentinel.response)

        first = asyncio.run(middleware.awrap_model_call(request, handler))
        second = asyncio.run(middleware.awrap_model_call(request, handler))

        self.assertIs(first, sentinel.response)
        self.assertIs(second, sentinel.response)
        self.assertEqual(handler.await_count, 2)
        selected = [event for event in events if event.event == "model_selected"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].data["model_id"], "agnes/agnes-2.0-flash")
        self.assertEqual(middleware.last_model_id, "agnes/agnes-2.0-flash")

    def test_static_router_leases_once_for_missing_selection(self) -> None:
        selection = self._selection()
        provider = SimpleNamespace(lease=AsyncMock(return_value=selection))
        middleware = StaticModelRouter(provider, role="AnswerWriter")
        request = MagicMock(tools=[], response_format=None, messages=[])
        handler = AsyncMock(return_value=sentinel.response)

        asyncio.run(middleware.awrap_model_call(request, handler))
        asyncio.run(middleware.awrap_model_call(request, handler))

        provider.lease.assert_awaited_once()
        self.assertEqual(middleware.last_model_id, "agnes/agnes-2.0-flash")

    def test_static_router_reject_logs_without_rotating(self) -> None:
        middleware = StaticModelRouter(
            SimpleNamespace(),
            role="orchestrator",
            selection=self._selection(),
        )
        middleware.reject_active_response(
            ToolProtocolViolation("tool_protocol_failure: no progress")
        )
        self.assertEqual(middleware.last_model_id, "agnes/agnes-2.0-flash")

    async def test_static_router_emits_call_start_and_content_events(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        middleware = StaticModelRouter(
            SimpleNamespace(),
            role="AnswerWriter",
            selection=self._selection(),
            sink=Sink(),
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

    async def test_static_router_emits_model_failed_when_handler_raises(self) -> None:
        events: list[FrameworkTraceEvent] = []

        class Sink:
            async def accept(self, event: object) -> None:
                events.append(cast(FrameworkTraceEvent, event))

        middleware = StaticModelRouter(
            SimpleNamespace(),
            role="AnswerWriter",
            selection=self._selection(),
            sink=Sink(),
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

    async def test_static_router_stamps_cache_markers_for_opencodego(self) -> None:
        provider = OpenCodeGoProvider(api_key="sk-test", model="deepseek-v4-flash")
        middleware = StaticModelRouter(provider, role="AnswerWriter", selection=self._selection())
        seen: dict[str, Any] = {}

        async def capture(req: Any) -> Any:
            seen["messages"] = req.messages
            return sentinel.response

        request = MagicMock(
            tools=[],
            response_format=None,
            messages=[
                SystemMessage(content="system prompt"),
                HumanMessage(content="first question"),
                HumanMessage(content="latest question"),
            ],
        )
        request.override.side_effect = lambda **values: SimpleNamespace(**values)

        await middleware.awrap_model_call(request, capture)

        messages = seen["messages"]
        self.assertIsInstance(messages[0].content, list)
        self.assertEqual(
            messages[0].content[0]["cache_control"], {"type": "ephemeral", "ttl": "1h"}
        )
        self.assertIsInstance(messages[1].content, str)
        self.assertEqual(messages[1].content, "first question")
        self.assertEqual(
            messages[2].content[0]["cache_control"], {"type": "ephemeral", "ttl": "1h"}
        )

    def test_stamp_prompt_cache_markers_leaves_other_roles_untouched(self) -> None:
        messages = [
            SystemMessage(content="system prompt"),
            AIMessage(content="assistant answer"),
            ToolMessage(content="tool result", tool_call_id="c1"),
            HumanMessage(content="latest question"),
        ]
        stamped = _stamp_prompt_cache_markers(messages)
        self.assertIsNotNone(stamped)
        self.assertIsInstance(stamped[0].content, list)
        self.assertEqual(stamped[1].content, "assistant answer")
        self.assertEqual(stamped[2].content, "tool result")
        self.assertIsInstance(stamped[3].content, list)
        self.assertIsNone(
            _stamp_prompt_cache_markers([ToolMessage(content="x", tool_call_id="c1")])
        )


if __name__ == "__main__":
    unittest.main()


class LedgerAppendTests(unittest.IsolatedAsyncioTestCase):
    def _selection(self) -> Any:
        return cast(
            Any,
            SimpleNamespace(
                info=SimpleNamespace(id="agnes/agnes-2.0-flash"),
                llm=MagicMock(),
            ),
        )

    def test_static_router_appends_each_successful_call_to_ledger(self) -> None:
        from z_apply_core.context.call_ledger import RunCallLedger

        ledger = RunCallLedger(job_url="https://example.test/job")
        middleware = StaticModelRouter(
            SimpleNamespace(),
            role="orchestrator",
            selection=self._selection(),
            ledger=ledger,
        )
        request = MagicMock(tools=[], response_format=None, messages=[])
        handler = AsyncMock(return_value=sentinel.response)

        asyncio.run(middleware.awrap_model_call(request, handler))
        asyncio.run(middleware.awrap_model_call(request, handler))

        self.assertEqual(ledger.call_count, 2)
        self.assertEqual([entry.sequence for entry in ledger.entries], [1, 2])
        self.assertTrue(all(entry.agent == "orchestrator" for entry in ledger.entries))
