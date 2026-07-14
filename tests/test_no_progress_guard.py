from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from z_apply_core.agents.no_progress_guard import NoProgressGuardMiddleware


@pytest.mark.asyncio
async def test_repeated_denials_rotate_model_without_escaping_agent() -> None:
    failures: list[Exception] = []
    middleware = NoProgressGuardMiddleware(on_no_progress=failures.append)
    request = SimpleNamespace(tool_call={"name": "ls", "args": {"path": "/"}, "id": "call-1"})

    async def denied(_request: object) -> ToolMessage:
        return ToolMessage(
            content="Error: permission denied for read on /",
            name="ls",
            tool_call_id="call-1",
            status="error",
        )

    first = await middleware.awrap_tool_call(request, denied)  # type: ignore[arg-type]
    second = await middleware.awrap_tool_call(request, denied)  # type: ignore[arg-type]

    assert first.content == "Error: permission denied for read on /"
    assert "active model was rotated" in str(second.content)
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_identical_successful_read_is_not_executed_twice() -> None:
    middleware = NoProgressGuardMiddleware()
    request = SimpleNamespace(
        tool_call={
            "name": "lookup_candidate_memory",
            "args": {"field_label": "Current Salary", "question": "Current Salary"},
            "id": "call-1",
        }
    )
    executions = 0

    async def lookup(_request: object) -> ToolMessage:
        nonlocal executions
        executions += 1
        return ToolMessage(
            content='{"memory_status":"exact","answer":"600000"}',
            name="lookup_candidate_memory",
            tool_call_id="call-1",
        )

    first = await middleware.awrap_tool_call(request, lookup)  # type: ignore[arg-type]
    second = await middleware.awrap_tool_call(request, lookup)  # type: ignore[arg-type]

    assert first.status == "success"
    assert second.status == "error"
    assert "already succeeded" in str(second.content)
    assert executions == 1


@pytest.mark.asyncio
async def test_successful_mutation_allows_same_read_again() -> None:
    middleware = NoProgressGuardMiddleware()
    read = SimpleNamespace(
        tool_call={"name": "browser_snapshot", "args": {}, "id": "read-1"}
    )
    mutation = SimpleNamespace(
        tool_call={"name": "browser_fill_form", "args": {}, "id": "write-1"}
    )
    executions = 0

    async def success(request: object) -> ToolMessage:
        nonlocal executions
        executions += 1
        call = request.tool_call  # type: ignore[attr-defined]
        return ToolMessage(
            content="success",
            name=str(call["name"]),
            tool_call_id=str(call["id"]),
        )

    await middleware.awrap_tool_call(read, success)  # type: ignore[arg-type]
    await middleware.awrap_tool_call(mutation, success)  # type: ignore[arg-type]
    repeated = await middleware.awrap_tool_call(read, success)  # type: ignore[arg-type]

    assert repeated.status == "success"
    assert executions == 3
