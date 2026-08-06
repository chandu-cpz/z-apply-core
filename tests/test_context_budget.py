from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from z_apply_core.context.context_budget import (
    ContextBudgetMiddleware,
    estimate_messages_tokens,
    estimate_tokens,
)


def _long_content() -> str:
    return "payload " * 400 + "\nrevision: " + "trailing " * 50


def _tool_message(rev: int, tool_call_id: str) -> ToolMessage:
    return ToolMessage(
        content=_long_content(),
        tool_call_id=tool_call_id,
        additional_kwargs={"browser_revision": rev},
    )


def _messages() -> list[Any]:
    task = HumanMessage(content="Apply to the role at example.com")
    controller_1 = HumanMessage(content="controller guidance one", name="controller")
    controller_2 = HumanMessage(content="controller guidance two", name="controller")
    mid_system = SystemMessage(content="injected earlier system text")
    tools = [
        _tool_message(1, "tool-1"),
        _tool_message(2, "tool-2"),
        _tool_message(3, "tool-3"),
        _tool_message(4, "tool-4"),
        _tool_message(505, "tool-5"),
        _tool_message(506, "tool-6"),
        _tool_message(507, "tool-7"),
        _tool_message(508, "tool-8"),
        _tool_message(509, "tool-9"),
        _tool_message(510, "tool-10"),
        ToolMessage(content="done revision: 511", tool_call_id="tool-11"),
        ToolMessage(content="done revision: 512", tool_call_id="tool-12"),
    ]
    final_system = SystemMessage(content="final system text")
    final_controller = HumanMessage(content="final controller note", name="controller")
    return [task, controller_1, controller_2, mid_system, *tools, final_system, final_controller]


def test_estimate_helpers() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_messages_tokens([HumanMessage(content="abcd")]) == 4


@pytest.mark.asyncio
async def test_compaction_drops_oldest_tools_and_projects_long_revision_messages() -> None:
    original = _messages()
    request = ModelRequest(model=object(), messages=original, tools=[])
    middleware = ContextBudgetMiddleware(budget_tokens=1000, keep_tool_messages=8)
    received: list[ModelRequest[Any]] = []

    async def handler(req: ModelRequest[Any]) -> ModelResponse[Any]:
        received.append(req)
        return ModelResponse(result=[])

    await middleware.awrap_model_call(request, handler)

    assert len(received) == 1
    assert request.messages == original
    compacted = received[0].messages

    tool_ids = [
        message.tool_call_id for message in compacted if isinstance(message, ToolMessage)
    ]
    assert len(tool_ids) == 8
    assert "tool-1" not in tool_ids
    assert "tool-4" not in tool_ids
    assert "tool-5" in tool_ids
    assert "tool-12" in tool_ids
    assert tool_ids == [f"tool-{index}" for index in range(5, 13)]

    assert isinstance(compacted[0], HumanMessage)
    assert compacted[0].name is None
    assert compacted[0].content == "Apply to the role at example.com"

    system_contents = [
        message.content for message in compacted if isinstance(message, SystemMessage)
    ]
    assert system_contents == ["final system text"]

    tool_5 = next(
        message
        for message in compacted
        if isinstance(message, ToolMessage) and message.tool_call_id == "tool-5"
    )
    assert tool_5.content == (
        "[evidence truncated — full revision 505 in evidence store; use browser_find]"
    )
    assert tool_5.tool_call_id == "tool-5"

    tool_11 = next(
        message
        for message in compacted
        if isinstance(message, ToolMessage) and message.tool_call_id == "tool-11"
    )
    assert tool_11.content == "done revision: 511"


@pytest.mark.asyncio
async def test_long_message_without_revision_gets_generic_truncation() -> None:
    messages = [
        HumanMessage(content="task"),
        ToolMessage(content="payload " * 500, tool_call_id="no-rev"),
    ]
    request = ModelRequest(model=object(), messages=messages, tools=[])
    middleware = ContextBudgetMiddleware(budget_tokens=1, keep_tool_messages=8)
    received: list[ModelRequest[Any]] = []

    async def handler(req: ModelRequest[Any]) -> ModelResponse[Any]:
        received.append(req)
        return ModelResponse(result=[])

    await middleware.awrap_model_call(request, handler)

    tool = next(
        message for message in received[0].messages if isinstance(message, ToolMessage)
    )
    assert tool.content == "[tool result truncated to fit context budget]"
    assert tool.tool_call_id == "no-rev"


@pytest.mark.asyncio
async def test_under_budget_passes_through_with_same_message_list() -> None:
    small = [
        HumanMessage(content="task"),
        ToolMessage(content="short", tool_call_id="t1"),
        ToolMessage(content="short", tool_call_id="t2"),
        SystemMessage(content="sys"),
    ]
    request = ModelRequest(model=object(), messages=small, tools=[])
    middleware = ContextBudgetMiddleware(budget_tokens=100_000, keep_tool_messages=8)
    received: list[ModelRequest[Any]] = []

    async def handler(req: ModelRequest[Any]) -> ModelResponse[Any]:
        received.append(req)
        return ModelResponse(result=[])

    await middleware.awrap_model_call(request, handler)

    assert len(received) == 1
    assert received[0] is request
    assert received[0].messages is request.messages
