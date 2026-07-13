from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from z_apply_core.agents.no_progress_guard import NoProgressGuardMiddleware


@pytest.mark.asyncio
async def test_repeated_denials_rotate_model_without_escaping_agent() -> None:
    failures: list[Exception] = []
    middleware = NoProgressGuardMiddleware(on_no_progress=failures.append)
    request = SimpleNamespace(
        tool_call={"name": "ls", "args": {"path": "/"}, "id": "call-1"}
    )

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
