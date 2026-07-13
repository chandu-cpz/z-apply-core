from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

MAX_PARALLEL_ANSWER_WRITERS = 8


class SafeToolBatchMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Parallelize read-only candidate resolution while serializing side effects."""

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

        if all(self._is_answer_writer_task(call) for call in message.tool_calls):
            allowed = message.tool_calls[:MAX_PARALLEL_ANSWER_WRITERS]
            logger.info(
                "SafeToolBatch: executing %s AnswerWriter tasks concurrently",
                len(allowed),
            )
            return message.model_copy(update={"tool_calls": allowed})

        logger.info(
            "SafeToolBatch: serializing mixed or side-effecting batch of %s tools; "
            "executing only %s",
            len(message.tool_calls),
            message.tool_calls[0].get("name", "unknown"),
        )
        return message.model_copy(update={"tool_calls": [message.tool_calls[0]]})

    @staticmethod
    def _is_answer_writer_task(call: Mapping[str, Any]) -> bool:
        if call.get("name") != "task":
            return False
        args = call.get("args")
        return isinstance(args, dict) and args.get("subagent_type") == "AnswerWriter"
