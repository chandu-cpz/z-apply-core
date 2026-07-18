from __future__ import annotations

from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import ToolMessage

_CANDIDATE_MEMORY_TOOL = "lookup_candidate_memory"


class RequireNativeToolCallMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Require candidate memory before exposing structured AnswerWriter output."""

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]],
            Awaitable[ModelResponse[ResponseT]],
        ],
    ) -> ModelResponse[ResponseT]:
        memory_complete = any(
            isinstance(message, ToolMessage) and message.name == _CANDIDATE_MEMORY_TOOL
            for message in request.messages
        )
        if memory_complete:
            return await handler(request)

        memory_tool = next(
            (
                tool
                for tool in request.tools
                if getattr(tool, "name", None) == _CANDIDATE_MEMORY_TOOL
            ),
            None,
        )
        if memory_tool is None:
            return await handler(request)

        return await handler(
            request.override(
                tools=[memory_tool],
                tool_choice=_CANDIDATE_MEMORY_TOOL,
                response_format=None,
            )
        )
