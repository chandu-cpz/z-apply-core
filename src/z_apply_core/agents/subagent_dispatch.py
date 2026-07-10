from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage


class SubagentDispatchMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Normalize a model's direct subagent call into DeepAgents' ``task`` call.

    Subagent names are agent types, not callable tools. Some tool-capable
    providers nevertheless emit a tool call using the displayed agent name.
    This middleware translates that typed tool-call intent before tool
    execution, keeping browser authority inside BrowserSpecialist.
    """

    def __init__(self, subagent_types: Iterable[str]) -> None:
        super().__init__()
        self._subagent_types = frozenset(subagent_types)

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
        if normalized == message.tool_calls:
            return message
        return message.model_copy(update={"tool_calls": normalized})

    def _normalize_call(self, call: Mapping[str, Any]) -> dict[str, Any]:
        subagent_type = call.get("name")
        if not isinstance(subagent_type, str) or subagent_type not in self._subagent_types:
            return dict(call)

        args = call.get("args")
        description = args.get("description") if isinstance(args, dict) else None
        if not isinstance(description, str) or not description.strip():
            description = (
                f"Complete the one bounded {subagent_type} task requested by the parent. "
                "Return only evidence relevant to that task."
            )
        return {
            **call,
            "name": "task",
            "args": {"subagent_type": subagent_type, "description": description},
        }
