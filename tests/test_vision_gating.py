from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware
from z_apply_core.agents.vision_message_compat import (
    VisionToolMessageCompatibilityMiddleware,
)
from z_apply_core.browser_observation import BrowserCapabilities


@pytest.mark.asyncio
async def test_vision_delegation_is_denied_on_dom_operable_page() -> None:
    browser = SimpleNamespace(
        inspect_capabilities=AsyncMock(
            return_value=BrowserCapabilities(editable_controls_visible=True)
        )
    )
    middleware = SubagentDispatchMiddleware(["VisionSpecialist"], browser=browser)
    request = SimpleNamespace(
        tool_call={
            "name": "task",
            "args": {"subagent_type": "VisionSpecialist", "description": "Inspect page"},
            "id": "vision-1",
        }
    )
    handler = AsyncMock()

    result = await middleware.awrap_tool_call(request, handler)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_vision_delegation_runs_on_visual_only_surface() -> None:
    browser = SimpleNamespace(
        inspect_capabilities=AsyncMock(
            return_value=BrowserCapabilities(visual_only_surface_visible=True)
        )
    )
    middleware = SubagentDispatchMiddleware(["VisionSpecialist"], browser=browser)
    request = SimpleNamespace(
        tool_call={
            "name": "task",
            "args": {"subagent_type": "VisionSpecialist", "description": "Read diagram"},
            "id": "vision-2",
        }
    )
    expected = ToolMessage(content="observed", tool_call_id="vision-2")
    handler = AsyncMock(return_value=expected)

    result = await middleware.awrap_tool_call(request, handler)

    assert result is expected
    handler.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_vision_images_are_not_sent_inside_nim_tool_messages() -> None:
    middleware = VisionToolMessageCompatibilityMiddleware()
    request = ModelRequest(
        model=object(),
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"name": "browser_take_screenshot", "args": {}, "id": "shot"}],
            ),
            ToolMessage(
                tool_call_id="shot",
                content=[
                    {"type": "text", "text": "Screenshot captured"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                ],
            ),
        ],
        tools=[],
    )

    async def handler(received: ModelRequest[Any]) -> ModelResponse[Any]:
        tool_message = received.messages[-2]
        image_message = received.messages[-1]
        assert isinstance(tool_message, ToolMessage)
        assert all(block.get("type") != "image_url" for block in tool_message.content)
        assert isinstance(image_message, HumanMessage)
        assert any(block.get("type") == "image_url" for block in image_message.content)
        return ModelResponse(result=[AIMessage(content="done")])

    response = await middleware.awrap_model_call(request, handler)

    assert response.result[0].text == "done"
