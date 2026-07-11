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
from z_apply_core.stream_events import FrameworkEventSink


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
        sink: FrameworkEventSink | None = None,
    ) -> None:
        super().__init__()
        self._snapshot_tool = next(
            (tool for tool in read_only_browser_tools if tool.name == "browser_snapshot"),
            None,
        )
        self._verifier = create_deep_agent(
            model=fallback_model,
            tools=list(read_only_browser_tools),
            system_prompt=load_prompt(prompt_name),
            middleware=[NimRouterMiddleware(router, role=verifier_role)],
        )
        self._sink = sink

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name = str(request.tool_call.get("name", ""))
        if tool_name not in BROWSER_CHANGING_TOOL_NAMES:
            return await handler(request)

        arguments = request.tool_call.get("args", {})
        verification_goal = (
            arguments.get("verification_goal") if isinstance(arguments, dict) else None
        )
        if not isinstance(verification_goal, str) or not verification_goal.strip():
            return ToolMessage(
                content=(
                    "Browser mutation rejected: verification_goal must name the "
                    "semantic operation and its visible success condition."
                ),
                tool_call_id=str(request.tool_call.get("id", "")),
                name=tool_name,
                status="error",
            )

        result = await handler(request)
        if not isinstance(result, ToolMessage) or result.status == "error":
            return result

        verdict = await self._verify(
            tool_name=tool_name,
            arguments={
                key: value for key, value in arguments.items() if key != "verification_goal"
            },
            verification_goal=verification_goal.strip(),
            action_output=str(result.content),
            snapshot=await self._fresh_snapshot(),
        )
        return result.model_copy(
            update={
                "content": (
                    f"{result.content}\n\n"
                    f"VERIFICATION_GOAL: {verification_goal.strip()}\n"
                    f"AUTOMATIC_VERIFIER_RESULT: {verdict}"
                )
            }
        )

    async def _verify(
        self,
        *,
        tool_name: str,
        arguments: object,
        verification_goal: str,
        action_output: str,
        snapshot: str,
    ) -> str:
        try:
            stream = self._verifier.astream_events(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Independently verify the browser action that just completed. "
                                f"Semantic verification goal: {verification_goal}. "
                                f"Low-level action: {tool_name}. Arguments: {arguments!r}. "
                                f"Tool output: {action_output}. "
                                f"Fresh browser snapshot captured by the runtime:\n{snapshot}\n\n"
                                "Element refs in the low-level action belong to the pre-action "
                                "snapshot and may identify different elements now. Do not look "
                                "up or reinterpret those old refs in the fresh snapshot. Verify "
                                "only whether the named semantic goal is now satisfied. "
                                "Return the required verdict format based on that evidence."
                            ),
                        }
                    ]
                },
                version="v3",
            )
            run = await consume_deepagent_stream(
                stream,
                sink=self._sink,
                root_source="AuthActionVerifier",
            )
        except Exception as exc:  # noqa: BLE001 - verdict is returned to the browser agent
            return f"not_verified: automatic verifier failed: {exc}"
        return _last_message_text(run.output) or "not_verified: verifier returned no result."

    async def _fresh_snapshot(self) -> str:
        if self._snapshot_tool is None:
            return "Snapshot unavailable: no browser_snapshot tool was configured."
        try:
            return str(await self._snapshot_tool.ainvoke({}))
        except Exception as exc:  # noqa: BLE001 - passed to verifier as failed evidence
            return f"Snapshot unavailable: {exc}"


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
