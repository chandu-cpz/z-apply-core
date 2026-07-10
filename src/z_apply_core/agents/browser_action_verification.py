from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from nim_router import NimRouter

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.browser_tools import BROWSER_CHANGING_TOOL_NAMES


class BrowserActionVerificationMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Run an independent read-only verifier after every browser mutation.

    The browser specialist cannot opt out: verification is attached to the
    browser tool execution path itself, rather than being left to an
    orchestrator prompt or final trace audit.
    """

    def __init__(
        self,
        *,
        fallback_model: BaseChatModel,
        router: NimRouter,
        read_only_browser_tools: Sequence[BaseTool],
        prompt_name: str,
        verifier_role: str,
    ) -> None:
        super().__init__()
        self._verifier = create_deep_agent(
            model=fallback_model,
            tools=list(read_only_browser_tools),
            system_prompt=load_prompt(prompt_name),
            middleware=[NimRouterMiddleware(router, role=verifier_role)],
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result
        tool_name = str(request.tool_call.get("name", ""))
        if tool_name not in BROWSER_CHANGING_TOOL_NAMES or result.status == "error":
            return result

        verdict = await self._verify(
            tool_name=tool_name,
            arguments=request.tool_call.get("args", {}),
            action_output=str(result.content),
        )
        return result.model_copy(
            update={
                "content": (
                    f"{result.content}\n\n"
                    f"AUTOMATIC_VERIFIER_RESULT: {verdict}"
                )
            }
        )

    async def _verify(
        self,
        *,
        tool_name: str,
        arguments: object,
        action_output: str,
    ) -> str:
        try:
            stream = self._verifier.astream_events(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Independently verify the browser action that just completed. "
                                f"Action: {tool_name}. Arguments: {arguments!r}. "
                                f"Tool output: {action_output}. "
                                "Use fresh read-only browser evidence and return the required "
                                "verdict format."
                            ),
                        }
                    ]
                },
                version="v3",
            )
            run = await consume_deepagent_stream(stream)
        except Exception as exc:  # noqa: BLE001 - verdict is returned to the browser agent
            return f"not_verified: automatic verifier failed: {exc}"
        return _last_message_text(run.output) or "not_verified: verifier returned no result."


def _last_message_text(output: dict[str, Any]) -> str:
    messages = output.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if parts:
                return "\n".join(parts).strip()
    return ""
