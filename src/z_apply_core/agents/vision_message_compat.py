from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import HumanMessage, ToolMessage


class VisionToolMessageCompatibilityMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Move image blocks out of ToolMessage for NIM-compatible vision turns."""

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        messages: list[Any] = []
        for message in request.messages:
            if not isinstance(message, ToolMessage) or not isinstance(message.content, list):
                messages.append(message)
                continue
            text_blocks: list[dict[str, Any]] = []
            image_blocks: list[dict[str, Any]] = []
            for block in message.content:
                if isinstance(block, dict) and block.get("type") in {"image", "image_url"}:
                    image_blocks.append(block)
                elif isinstance(block, dict):
                    text_blocks.append(block)
            if not image_blocks:
                messages.append(message)
                continue
            messages.append(message.model_copy(update={"content": text_blocks or "Screenshot captured."}))
            messages.append(
                HumanMessage(
                    content=[
                        {"type": "text", "text": "Current screenshot for the bounded visual question."},
                        *image_blocks,
                    ]
                )
            )
        return await handler(request.override(messages=messages))
