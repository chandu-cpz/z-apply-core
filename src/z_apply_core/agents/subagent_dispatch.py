from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from z_apply_core.browser_session import BrowserSession


class SubagentDispatchMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Normalize direct specialist calls and guard visual-only delegation."""

    def __init__(
        self,
        subagent_types: Iterable[str],
        *,
        resume_path: str = "",
        browser: BrowserSession | None = None,
    ) -> None:
        super().__init__()
        self._subagent_types = frozenset(subagent_types)
        self._resume_path = resume_path
        self._browser = browser

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        args = request.tool_call.get("args", {})
        if (
            request.tool_call.get("name") == "task"
            and isinstance(args, dict)
            and args.get("subagent_type") == "VisionSpecialist"
        ):
            browser = self._browser
            capabilities = await browser.inspect_capabilities() if browser is not None else None
            if capabilities is None or not capabilities.visual_only_surface_visible:
                return ToolMessage(
                    content=(
                        "Vision delegation denied: current browser-owned capabilities do "
                        "not show a visual-only surface. Continue with current ARIA/DOM "
                        "evidence and browser tools. Visual CAPTCHA or identity challenges "
                        "go directly to human HITL."
                    ),
                    name="task",
                    tool_call_id=str(request.tool_call.get("id", "")),
                    status="error",
                )
        return await handler(request)

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
        args = call.get("args")
        subagent_type = call.get("name")
        if not isinstance(subagent_type, str) or subagent_type not in self._subagent_types:
            if subagent_type == "task" and isinstance(args, dict):
                nested = args.get("subagent_type")
                if isinstance(nested, str) and nested in self._subagent_types:
                    description = args.get("description", "")
                    if isinstance(description, str):
                        description = self._normalize_description(nested, description)
                        return {**call, "args": {**args, "description": description}}
            return dict(call)

        description = args.get("description") if isinstance(args, dict) else None
        if not isinstance(description, str) or not description.strip():
            description = (
                f"Complete the one bounded {subagent_type} task requested by the parent. "
                "Return only evidence relevant to that task."
            )
        return {
            **call,
            "name": "task",
            "args": {
                "subagent_type": subagent_type,
                "description": self._normalize_description(subagent_type, description),
            },
        }

    def _normalize_description(self, subagent_type: str, description: str) -> str:
        normalized = (
            description.replace("RESUME_PATH", self._resume_path)
            if self._resume_path
            else description
        )
        if subagent_type != "AuthenticationSpecialist":
            return normalized
        return (
            "RUNTIME AUTHENTICATION OBJECTIVE: Resolve the currently visible gate. "
            "The parent description below is starting evidence, not authorization to "
            "skip the fixed login -> account creation -> password reset recovery order. "
            "Fresh browser evidence supersedes its refs and selected panel after every "
            "action.\n\nPARENT HANDOFF EVIDENCE:\n"
            f"{normalized}"
        )
