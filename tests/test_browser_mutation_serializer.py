from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from z_apply_core.agents.browser_mutation_serializer import SerializeBrowserMutationsMiddleware


def _request(call_id: str, name: str = "browser_click") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "id": call_id, "args": {}},
        tool=None,
        state=None,
        runtime=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_failed_batch_mutation_skips_remaining_mutations() -> None:
    middleware = SerializeBrowserMutationsMiddleware()
    middleware._batch_mutation_ids = {"call-1", "call-2", "call-3"}
    middleware._batch_failed = False
    executed: list[str] = []

    async def handler(request: ToolCallRequest) -> ToolMessage:
        call_id = str(request.tool_call["id"])
        executed.append(call_id)
        if call_id == "call-1":
            return ToolMessage(
                content="browser_click failed",
                tool_call_id=call_id,
                name="browser_click",
                status="error",
            )
        return ToolMessage(content="ok", tool_call_id=call_id, name="browser_click")

    first = await middleware.awrap_tool_call(_request("call-1"), handler)
    assert isinstance(first, ToolMessage)
    assert first.status == "error"
    assert middleware._batch_failed is True

    second = await middleware.awrap_tool_call(_request("call-2"), handler)
    assert isinstance(second, ToolMessage)
    assert second.status == "error"
    assert "SKIPPED" in str(second.content)
    assert second.tool_call_id == "call-2"

    third = await middleware.awrap_tool_call(_request("call-3"), handler)
    assert isinstance(third, ToolMessage)
    assert "SKIPPED" in str(third.content)

    assert executed == ["call-1"]


@pytest.mark.asyncio
async def test_successful_batch_runs_all_mutations() -> None:
    middleware = SerializeBrowserMutationsMiddleware()
    middleware._batch_mutation_ids = {"call-1", "call-2"}
    middleware._batch_failed = False
    executed: list[str] = []

    async def handler(request: ToolCallRequest) -> ToolMessage:
        call_id = str(request.tool_call["id"])
        executed.append(call_id)
        return ToolMessage(content="ok", tool_call_id=call_id, name="browser_click")

    await middleware.awrap_tool_call(_request("call-1"), handler)
    await middleware.awrap_tool_call(_request("call-2"), handler)

    assert executed == ["call-1", "call-2"]
    assert middleware._batch_failed is False


@pytest.mark.asyncio
async def test_single_failed_mutation_outside_batch_is_not_skipped() -> None:
    middleware = SerializeBrowserMutationsMiddleware()
    middleware._batch_mutation_ids = set()
    middleware._batch_failed = False
    executed: list[str] = []

    async def handler(request: ToolCallRequest) -> ToolMessage:
        executed.append(str(request.tool_call["id"]))
        return ToolMessage(
            content="browser_click failed",
            tool_call_id=str(request.tool_call["id"]),
            name="browser_click",
            status="error",
        )

    result = await middleware.awrap_tool_call(_request("call-1"), handler)

    assert executed == ["call-1"]
    assert isinstance(result, ToolMessage)
    assert "SKIPPED" not in str(result.content)
