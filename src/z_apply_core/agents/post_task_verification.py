from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

_log = logging.getLogger(__name__)

VERIFIER_SOURCE = "PostTaskVerifier"


@dataclass(frozen=True)
class SnapshotEvidence:
    content: str
    collected: bool


class PostTaskVerificationMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Pair each native BrowserSpecialist task with a native Verifier task.

    DeepAgents' built-in ``task`` handler runs both subagents. The browser task
    completes first, then the verifier receives its result and a fresh browser
    snapshot. Both reports are returned in the original task result so the
    orchestrator remains responsible for the next application-flow decision.
    """

    def __init__(
        self,
        *,
        read_only_browser_tools: Sequence[BaseTool],
        target_subagent: str = "BrowserSpecialist",
        verifier_subagent: str = "Verifier",
        sink: FrameworkEventSink | None = None,
    ) -> None:
        super().__init__()
        self._target_subagent = target_subagent
        self._verifier_subagent = verifier_subagent
        self._snapshot_tool = next(
            (tool for tool in read_only_browser_tools if tool.name == "browser_snapshot"),
            None,
        )
        self._sink = sink

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if str(request.tool_call.get("name", "")) != "task":
            return await handler(request)

        arguments = request.tool_call.get("args", {})
        if not isinstance(arguments, dict):
            return await handler(request)
        subagent_type = str(arguments.get("subagent_type", "specialist"))
        if subagent_type != self._target_subagent:
            try:
                return await handler(request)
            except Exception as exc:  # noqa: BLE001 - orchestrator owns recovery
                _log.warning("Native %s task failed technically: %s", subagent_type, exc)
                return _technical_failure_message(request, subagent_type, exc)

        description = str(arguments.get("description", ""))
        _log.info("PostTaskVerification: running native %s task", self._target_subagent)
        try:
            browser_result = await handler(request)
        except Exception as exc:  # noqa: BLE001 - return control to the orchestrator
            _log.warning(
                "PostTaskVerification: native %s task failed technically: %s",
                self._target_subagent,
                exc,
            )
            recovered_result = await self._continue_after_browser_failure(
                request=request,
                handler=handler,
                description=description,
                failure=exc,
            )
            if recovered_result is None:
                return _technical_failure_message(request, self._target_subagent, exc)
            browser_result = recovered_result
        browser_message = _command_tool_message(browser_result)
        if browser_message is None:
            _log.warning(
                "PostTaskVerification: browser task returned no ToolMessage; verifier skipped"
            )
            return browser_result

        snapshot = await self._fresh_snapshot()
        if not snapshot.collected:
            recovery_description = (
                f"{description}\n\n"
                "CONTINUE THE SAME BROWSER OPERATION:\n"
                "The previous BrowserSpecialist returned before independent evidence "
                "could be collected. The read-only browser evidence tool produced this "
                f"actual result:\n{snapshot.content}\n\n"
                "Use that tool result and the current browser state to continue the "
                "original semantic operation. Do not restart it, repeat a completed "
                "mutation, or merely describe the next tool call. Call the browser tool "
                "needed to finish the operation, then inspect evidence and return."
            )
            recovery_call = cast(
                ToolCall,
                {
                    **request.tool_call,
                    "args": {
                        "description": recovery_description,
                        "subagent_type": self._target_subagent,
                    },
                },
            )
            _log.info(
                "PostTaskVerification: evidence unavailable; continuing native %s task",
                self._target_subagent,
            )
            try:
                browser_result = await handler(request.override(tool_call=recovery_call))
            except Exception as exc:  # noqa: BLE001 - orchestrator owns retry decisions
                _log.warning(
                    "PostTaskVerification: continued %s task failed technically: %s",
                    self._target_subagent,
                    exc,
                )
                return _technical_failure_message(request, self._target_subagent, exc)
            browser_message = _command_tool_message(browser_result)
            if browser_message is None:
                _log.warning(
                    "PostTaskVerification: continued browser task returned no ToolMessage; "
                    "verifier skipped"
                )
                return browser_result
            snapshot = await self._fresh_snapshot()

        verifier_description = (
            "Independently verify the completed BrowserSpecialist task using current "
            "read-only browser evidence. Do not change browser state. Return your "
            "assessment and concrete evidence to the orchestrator, which owns the next "
            "decision.\n\n"
            f"ORIGINAL BROWSER TASK:\n{description}\n\n"
            f"BROWSER SPECIALIST RESULT:\n{browser_message.content}\n\n"
            f"FRESH POST-TASK SNAPSHOT:\n{snapshot.content}"
        )
        verifier_call = cast(
            ToolCall,
            {
                **request.tool_call,
                "args": {
                    "description": verifier_description,
                    "subagent_type": self._verifier_subagent,
                },
            },
        )

        _log.info("PostTaskVerification: running native %s task", self._verifier_subagent)
        try:
            verifier_result = await handler(request.override(tool_call=verifier_call))
        except Exception as exc:  # noqa: BLE001 - browser result remains authoritative evidence
            _log.warning("PostTaskVerification: native verifier task failed: %s", exc)
            verifier_content = f"VERIFIER_ERROR: {exc}"
        else:
            verifier_message = _command_tool_message(verifier_result)
            verifier_content = (
                str(verifier_message.content)
                if verifier_message is not None
                else "VERIFIER_ERROR: native Verifier task returned no ToolMessage."
            )

        combined = browser_message.model_copy(
            update={
                "content": (
                    f"BROWSER_SPECIALIST_RESULT:\n{browser_message.content}\n\n"
                    f"VERIFIER_RESULT:\n{verifier_content}"
                )
            }
        )
        base: dict[str, Any] = {}
        if isinstance(browser_result, Command) and isinstance(browser_result.update, dict):
            base = dict(browser_result.update)
        _log.info("PostTaskVerification: paired native task results returned")
        return Command(update={**base, "messages": [combined]})

    async def _continue_after_browser_failure(
        self,
        *,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        description: str,
        failure: Exception,
    ) -> ToolMessage | Command[Any] | None:
        recovery_description = (
            f"{description}\n\n"
            "CONTINUE THE SAME BROWSER OPERATION:\n"
            "The previous BrowserSpecialist turn ended with this actual typed browser "
            f"tool failure:\n{failure}\n\n"
            "Inspect the current browser state only when the tool is available. If a "
            "native file chooser or modal is already open, do not snapshot it, re-click "
            "the triggering control, or restart the operation. Continue with the browser "
            "tool that completes the original semantic operation."
        )
        recovery_call = cast(
            ToolCall,
            {
                **request.tool_call,
                "args": {
                    "description": recovery_description,
                    "subagent_type": self._target_subagent,
                },
            },
        )
        _log.info(
            "PostTaskVerification: continuing native %s after typed tool failure",
            self._target_subagent,
        )
        try:
            return await handler(request.override(tool_call=recovery_call))
        except Exception as exc:  # noqa: BLE001 - the orchestrator owns later recovery
            _log.warning(
                "PostTaskVerification: continued %s task failed technically: %s",
                self._target_subagent,
                exc,
            )
            return None

    async def _fresh_snapshot(self) -> SnapshotEvidence:
        if self._snapshot_tool is None:
            return SnapshotEvidence(
                "Snapshot unavailable: no browser_snapshot tool was configured.",
                collected=False,
            )
        await self._emit_snapshot_event("agent_tool_start", {"input": {}})
        try:
            snapshot = str(await self._snapshot_tool.ainvoke({}))
        except Exception as exc:  # noqa: BLE001 - verifier receives the evidence failure
            message = f"Snapshot unavailable: {exc}"
            await self._emit_snapshot_event(
                "agent_tool_end",
                {"output": "", "error": message, "completed": False},
            )
            return SnapshotEvidence(message, collected=False)
        await self._emit_snapshot_event(
            "agent_tool_end",
            {"output": snapshot, "error": "", "completed": True},
        )
        return SnapshotEvidence(snapshot, collected=True)

    async def _emit_snapshot_event(self, event: str, data: dict[str, Any]) -> None:
        if self._sink is None:
            return
        await self._sink.accept(
            FrameworkTraceEvent(
                event=event,
                name=VERIFIER_SOURCE,
                data={"tool_name": "browser_snapshot", **data},
                raw={},
            )
        )


def _command_tool_message(result: ToolMessage | Command[Any]) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return None
    messages = result.update.get("messages")
    if not isinstance(messages, list):
        return None
    return next((message for message in messages if isinstance(message, ToolMessage)), None)


def _technical_failure_message(
    request: ToolCallRequest,
    subagent_type: str,
    error: Exception,
) -> ToolMessage:
    return ToolMessage(
        content=(
            f"{subagent_type} task failed technically before its semantic operation could "
            f"be verified: {error}. Obtain current browser evidence and decide whether to "
            "retry the same operation or choose another safe recovery action."
        ),
        name="task",
        tool_call_id=str(request.tool_call.get("id", "")),
        status="error",
    )
