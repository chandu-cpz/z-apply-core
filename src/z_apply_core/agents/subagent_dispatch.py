from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)


class SubagentDispatchMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Normalize a model's direct subagent call into DeepAgents' ``task`` call.

    Subagent names are agent types, not callable tools. Some tool-capable
    providers nevertheless emit a tool call using the displayed agent name.
    This middleware translates that typed tool-call intent before tool
    execution, keeping browser authority inside BrowserSpecialist.
    """

    def __init__(self, subagent_types: Iterable[str], *, resume_path: str = "") -> None:
        super().__init__()
        self._subagent_types = frozenset(subagent_types)
        self._resume_path = resume_path

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        result: ModelResponse[ResponseT] = await handler(request)
        messages = [self._normalize_message(message) for message in result.result]
        return ModelResponse(result=messages, structured_response=result.structured_response)

    def _normalize_message(self, message: Any) -> Any:
        if not isinstance(message, AIMessage) or not message.tool_calls:
            return message

        normalized = [self._normalize_call(call) for call in message.tool_calls]
        task_calls = [call for call in normalized if call.get("name") == "task"]
        if len(task_calls) > 1:
            logger.info(
                "SubagentDispatch: serializing %s delegated tasks; executing the first",
                len(task_calls),
            )
            normalized = [task_calls[0]]
        if normalized == message.tool_calls:
            return message
        return message.model_copy(update={"tool_calls": normalized})

    def _normalize_call(self, call: Mapping[str, Any]) -> dict[str, Any]:
        args = call.get("args")
        subagent_type = call.get("name")
        if not isinstance(subagent_type, str) or subagent_type not in self._subagent_types:
            if subagent_type == "task" and isinstance(args, dict):
                nested = args.get("subagent_type")
                if isinstance(nested, str) and nested in self._subagent_types:
                    description = args.get("description", "")
                    if isinstance(description, str) and self._resume_path:
                        description = description.replace("RESUME_PATH", self._resume_path)
                        return {**call, "args": {**args, "description": description}}
            return dict(call)

        description = args.get("description") if isinstance(args, dict) else None
        if not isinstance(description, str) or not description.strip():
            description = (
                f"Complete the one bounded {subagent_type} task requested by the parent. "
                "Return only evidence relevant to that task."
            )
        if self._resume_path:
            description = description.replace("RESUME_PATH", self._resume_path)
        return {
            **call,
            "name": "task",
            "args": {"subagent_type": subagent_type, "description": description},
        }
