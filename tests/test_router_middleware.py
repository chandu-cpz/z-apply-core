from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, sentinel

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage
from nim_router import NimRouter
from nim_router.schemas import ModelSelection

from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.agents.router_middleware import (
    ORCHESTRATOR_EXCLUDED_MODEL_IDS,
    NimRouterMiddleware,
)


class RouterMiddlewareTests(unittest.IsolatedAsyncioTestCase):
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
            await NimRouterMiddleware(
                router, role="AuthenticationSpecialist"
            ).awrap_model_call(request, handler)

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

    async def test_removes_think_block_from_final_answer(self) -> None:
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

        self.assertEqual(result.result[0].content, "Gender = Male")

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
            reasoning=False,
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
            reasoning=False,
            priority="quality",
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


if __name__ == "__main__":
    unittest.main()
