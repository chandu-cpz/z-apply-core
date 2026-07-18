from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

class SafeToolBatchMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Serialize tool calls so human-capable specialists cannot race each other."""

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        response: ModelResponse[ResponseT] = await handler(request)
        normalized = [self._safe_batch(message) for message in response.result]
        return ModelResponse(
            result=normalized,
            structured_response=response.structured_response,
        )

    def _safe_batch(self, message: Any) -> Any:
        if not isinstance(message, AIMessage) or not message.tool_calls:
            return message

        if len(message.tool_calls) == 1:
            return message

        logger.info(
            "SafeToolBatch: serializing batch of %s tools; "
            "executing only %s",
            len(message.tool_calls),
            message.tool_calls[0].get("name", "unknown"),
        )
        return message.model_copy(update={"tool_calls": [message.tool_calls[0]]})
