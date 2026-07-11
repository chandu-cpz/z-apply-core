from __future__ import annotations

import logging
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

_log = logging.getLogger(__name__)


class PostTaskVerificationMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Run an independent verifier after BrowserSpecialist completes.

    Unlike the old per-mutation verification (which ran after every browser
    tool call inside BrowserSpecialist), this middleware runs ONCE after the
    entire BrowserSpecialist task completes. It checks whether the orchestrator's
    requested operation was accomplished, not whether individual clicks worked.

    The orchestrator receives both BrowserSpecialist's result and the
    verification verdict in a single tool result.
    """

    def __init__(
        self,
        *,
        fallback_model: BaseChatModel,
        router: NimRouter,
        read_only_browser_tools: Sequence[BaseTool],
        prompt_name: str = "verifier.md",
        verifier_role: str = "Verifier",
        target_subagent: str = "BrowserSpecialist",
    ) -> None:
        super().__init__()
        self._target_subagent = target_subagent
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

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name = str(request.tool_call.get("name", ""))
        _log.info("PostTaskVerification: awrap_tool_call invoked, tool=%s", tool_name)
        if tool_name != "task":
            return await handler(request)

        arguments = request.tool_call.get("args", {})
        if not isinstance(arguments, dict):
            _log.info("PostTaskVerification: args not dict, type=%s", type(arguments).__name__)
            return await handler(request)

        subagent_type = arguments.get("subagent_type")
        if subagent_type != self._target_subagent:
            _log.info(
                "PostTaskVerification: subagent_type=%s != target=%s, skipping",
                subagent_type, self._target_subagent,
            )
            return await handler(request)

        description = arguments.get("description", "")
        _log.info("PostTaskVerification: intercepted task for %s, calling handler", subagent_type)

        result = await handler(request)
        _log.info("PostTaskVerification: handler returned %s", type(result).__name__)
        if not isinstance(result, Command):
            return result

        _log.info("PostTaskVerification: running verifier for task=%s", description[:120])
        verdict = await self._verify(
            task_description=str(description),
            snapshot=await self._fresh_snapshot(),
        )
        _log.info("PostTaskVerification: verdict=%s", verdict[:200])

        messages = result.update.get("messages") if isinstance(result.update, dict) else None
        if not messages or not isinstance(messages[0], ToolMessage):
            keys = (
                list(result.update.keys())
                if isinstance(result.update, dict)
                else type(result.update)
            )
            _log.info("PostTaskVerification: no ToolMessage, keys=%s", keys)
            return result

        original = messages[0]
        modified_content = (
            f"{original.content}\n\n"
            f"VERIFICATION_GOAL: {description}\n"
            f"AUTOMATIC_VERIFIER_RESULT: {verdict}"
        )
        modified_msg = original.model_copy(update={"content": modified_content})
        _log.info("PostTaskVerification: appended verifier result to ToolMessage")
        return Command(update={**result.update, "messages": [modified_msg]})

    async def _verify(
        self,
        *,
        task_description: str,
        snapshot: str,
    ) -> str:
        try:
            stream = self._verifier.astream_events(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Verify whether the BrowserSpecialist accomplished the "
                                "orchestrator's requested operation. "
                                f"Task description: {task_description}. "
                                f"Fresh browser snapshot after BrowserSpecialist:\n"
                                f"{snapshot}\n\n"
                                "Evaluate only whether the named semantic operation and "
                                "success condition are satisfied. "
                                "Return the required verdict format based on that evidence."
                            ),
                        }
                    ]
                },
                version="v3",
            )
            run = await consume_deepagent_stream(stream)
        except Exception as exc:  # noqa: BLE001 - verdict is returned to the orchestrator
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
