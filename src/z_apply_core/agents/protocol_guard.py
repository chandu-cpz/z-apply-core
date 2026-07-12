from __future__ import annotations

import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage


class ToolProtocolViolation(RuntimeError):
    """A model claimed tool execution without emitting native tool calls."""


_TRANSCRIPT_MARKERS = re.compile(
    r"(?im)^\s*(?:task\s*\(|browser_(?:click|type|fill_form|select_option|file_upload|snapshot)\s*\(|"
    r"(?:authentication_(?:verified|blocked|not_verified)|outcome_(?:satisfied|needs_revision|blocked))\s*\(|"
    r"(?:FIELD_MAPPER|ANSWER_WRITER|BROWSER_SPECIALIST|VERIFIER|TOOL)_RESULT\s*:)",
)


class ProseToolCallGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Reject fabricated tool transcripts before they can enter agent state."""

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        result: ModelResponse[ResponseT] = await handler(request)
        if not request.tools:
            return result
        for message in result.result:
            if not isinstance(message, AIMessage) or message.tool_calls:
                continue
            if _TRANSCRIPT_MARKERS.search(_message_text(message.content)):
                raise ToolProtocolViolation(
                    "tool_protocol_failure: model emitted a fabricated tool or specialist "
                    "transcript without native tool calls"
                )
        return result


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""
