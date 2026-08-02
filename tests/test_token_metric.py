from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from z_apply_core.context.run_context import RunContext
from z_apply_core.context.token_metric import (
    TokenMetricMiddleware,
    TokenUsage,
    estimate_messages_tokens,
    estimate_schema_tokens,
)
from z_apply_core.stream_events import TokenUsageEvent


@dataclass(slots=True)
class StubTool:
    name: str
    description: str
    args_schema: dict[str, Any] | None = None


def test_estimate_schema_tokens_handles_dict_and_object_tools() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "browser_click",
                "description": "Click an element on the page",
                "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}},
            },
        },
        {
            "name": "browser_navigate",
            "description": "Navigate to a URL",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
        },
        StubTool(
            name="task",
            description="Delegate work to a specialist",
            args_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        ),
    ]
    assert estimate_schema_tokens(tools) > 0
    assert estimate_schema_tokens(tools[:2]) > 0
    assert estimate_schema_tokens([tools[2]]) > 0


def test_estimate_schema_tokens_never_raises_on_garbage() -> None:
    assert estimate_schema_tokens([None, "x", 5]) >= 0


def test_token_usage_str_renders_expected_single_line() -> None:
    assert str(TokenUsage(48213, 1240, 27, 24)) == (
        "prompt_tokens=48213 tool_schema_tokens=1240 messages=27 tools=24"
    )


@pytest.mark.asyncio
async def test_middleware_emits_one_event_and_tracks_before_and_after_usage() -> None:
    emitted: list[object] = []
    run_context = RunContext()
    middleware = TokenMetricMiddleware(run_context=run_context, emit=emitted.append)
    request = ModelRequest(
        model=object(),
        messages=[HumanMessage(content="hello world, please fill this form")],
        tools=[
            {
                "name": "browser_click",
                "description": "Click an element",
                "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}},
            }
        ],
    )

    async def handler(req: ModelRequest[Any]) -> ModelResponse[Any]:
        return ModelResponse(result=[AIMessage(content="ok, observing the form")])

    await middleware.awrap_model_call(request, handler)

    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, TokenUsageEvent)
    assert event.usage is middleware.last_after_usage
    assert run_context.token_usage is event.usage
    assert event.model == "object"

    before = middleware.last_before_usage
    after = middleware.last_after_usage
    assert before is not None
    assert after is not None
    assert before.prompt_tokens == estimate_messages_tokens(request.messages)
    assert before.message_count == 1
    assert before.tool_count == 1
    assert after.prompt_tokens > before.prompt_tokens
    assert after.message_count == before.message_count + 1


@pytest.mark.asyncio
async def test_model_exception_propagates_despite_metric_failure() -> None:
    boom = RuntimeError("model exploded")

    def bad_emit(_event: object) -> None:
        raise RuntimeError("emit exploded")

    middleware = TokenMetricMiddleware(emit=bad_emit)
    request = ModelRequest(model=object(), messages=[HumanMessage(content="hi")], tools=[])

    async def handler(_req: ModelRequest[Any]) -> ModelResponse[Any]:
        raise boom

    with pytest.raises(RuntimeError) as exc_info:
        await middleware.awrap_model_call(request, handler)
    assert exc_info.value is boom


@pytest.mark.asyncio
async def test_metric_emit_failure_is_swallowed_without_breaking_model_call() -> None:
    def bad_emit(_event: object) -> None:
        raise RuntimeError("emit exploded")

    middleware = TokenMetricMiddleware(emit=bad_emit)
    request = ModelRequest(model=object(), messages=[HumanMessage(content="hi")], tools=[])

    async def handler(_req: ModelRequest[Any]) -> ModelResponse[Any]:
        return ModelResponse(result=[])

    result = await middleware.awrap_model_call(request, handler)
    assert result.result == []


def test_run_context_note_usage_stores_and_retrieves() -> None:
    run_context = RunContext()
    assert run_context.token_usage is None
    usage = TokenUsage(prompt_tokens=10, tool_schema_tokens=4, message_count=3, tool_count=2)
    run_context.note_usage(usage)
    assert run_context.token_usage is usage
