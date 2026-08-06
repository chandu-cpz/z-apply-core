from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage

from z_apply_core.agents.tool_rewrite import replace_tool_calls

logger = logging.getLogger(__name__)

class SafeToolBatchMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Allow parallel candidate-resolution calls; serialize every other batch.

    A multi-tool batch whose calls are all candidate resolution (an AnswerWriter
    ``task`` call, or ``resolve_candidate_field`` before the candidate middleware
    rewrites it) is read-only with respect to the browser and may keep every call
    so the N AnswerWriter subagents run concurrently. Any other multi-tool batch
    keeps the serial guarantee: only the first call executes, so mutating browser
    tools and human-capable specialists never race each other.
    """

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

        if all(self._is_candidate_resolution_call(call) for call in message.tool_calls):
            logger.info(
                "SafeToolBatch: executing %s candidate-resolution calls concurrently",
                len(message.tool_calls),
            )
            return message

        logger.info(
            "SafeToolBatch: serializing batch of %s tools; "
            "executing only %s",
            len(message.tool_calls),
            message.tool_calls[0].get("name", "unknown"),
        )
        return replace_tool_calls(message, [message.tool_calls[0]])

    @staticmethod
    def _is_candidate_resolution_call(call: Mapping[str, Any]) -> bool:
        if call.get("name") == "resolve_candidate_field":
            return True
        if call.get("name") != "task":
            return False
        args = call.get("args")
        return isinstance(args, dict) and args.get("subagent_type") == "AnswerWriter"
