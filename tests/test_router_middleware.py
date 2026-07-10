from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, sentinel

from nim_router import NimRouter
from nim_router.schemas import ModelSelection

from z_apply_core.agents.router_middleware import NimRouterMiddleware


class RouterMiddlewareTests(unittest.IsolatedAsyncioTestCase):
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
        router.record_success.assert_called_once()

    async def test_leases_normally_after_initial_selection_is_consumed(self) -> None:
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

        router.lease.assert_awaited_once_with(
            tools=True,
            structured=False,
            vision=False,
            reasoning=False,
            priority="balanced",
        )
        request.override.assert_called_once_with(model=leased_model)


if __name__ == "__main__":
    unittest.main()
