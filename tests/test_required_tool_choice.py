from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from z_apply_core.agents.required_tool_choice import RequireNativeToolCallMiddleware


@pytest.mark.asyncio
async def test_required_tool_choice_reaches_model_binding() -> None:
    middleware = RequireNativeToolCallMiddleware()
    request = ModelRequest(model=object(), messages=[], tools=[])

    async def handler(received: ModelRequest[Any]) -> ModelResponse[Any]:
        assert received.tool_choice == "any"
        return ModelResponse(result=[AIMessage(content="")])

    response = await middleware.awrap_model_call(request, handler)

    assert len(response.result) == 1
