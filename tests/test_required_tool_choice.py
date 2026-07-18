from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from z_apply_core.agents.required_tool_choice import RequireNativeToolCallMiddleware


@pytest.mark.asyncio
async def test_candidate_memory_is_the_only_initial_tool() -> None:
    middleware = RequireNativeToolCallMiddleware()
    response_format = object()
    memory_tool = SimpleNamespace(name="lookup_candidate_memory")
    request = ModelRequest(
        model=object(),
        messages=[],
        tools=[memory_tool, SimpleNamespace(name="ask_human")],
        response_format=response_format,  # type: ignore[arg-type]
    )

    async def handler(received: ModelRequest[Any]) -> ModelResponse[Any]:
        assert received.tool_choice == "lookup_candidate_memory"
        assert received.tools == [memory_tool]
        assert received.response_format is None
        return ModelResponse(result=[AIMessage(content="")])

    await middleware.awrap_model_call(request, handler)

    completed = request.override(
        messages=[
            ToolMessage(
                content='{"memory_status":"no_exact_match"}',
                name="lookup_candidate_memory",
                tool_call_id="memory-1",
            )
        ]
    )

    async def final_handler(received: ModelRequest[Any]) -> ModelResponse[Any]:
        assert received.tool_choice is None
        assert received.tools == request.tools
        assert received.response_format is response_format
        return ModelResponse(result=[AIMessage(content="")])

    await middleware.awrap_model_call(completed, final_handler)
