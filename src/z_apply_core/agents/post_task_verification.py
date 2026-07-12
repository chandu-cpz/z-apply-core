from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

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
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command
from nim_router import NimRouter

from z_apply_core.agents.application_progress import ApplicationProgress
from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.terminal_guard import (
    TerminalDecisionGuardMiddleware,
    TerminalDecisionRecorded,
)
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

_log = logging.getLogger(__name__)
VERIFIER_SOURCE = "PostTaskVerifier"


@dataclass(frozen=True, slots=True)
class VerificationDecision:
    operation: Literal[
        "form_open",
        "resume_control",
        "resume_upload",
        "field_fill",
        "review_ready",
        "other",
    ]
    status: Literal["verified", "not_verified", "blocked"]
    evidence: str


class _VerdictState:
    decision: VerificationDecision | None = None


@dataclass(frozen=True)
class SnapshotEvidence:
    content: str
    collected: bool


class PostTaskVerificationMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Verify every BrowserSpecialist operation through a separate typed verifier."""

    def __init__(
        self,
        *,
        fallback_model: BaseChatModel,
        router: NimRouter,
        read_only_browser_tools: Sequence[BaseTool],
        progress: ApplicationProgress,
        target_subagent: str = "BrowserSpecialist",
        sink: FrameworkEventSink | None = None,
    ) -> None:
        super().__init__()
        self._fallback_model = fallback_model
        self._router = router
        self._tools = list(read_only_browser_tools)
        self._snapshot_tool = next(
            (item for item in self._tools if item.name == "browser_snapshot"), None
        )
        self._progress = progress
        self._target_subagent = target_subagent
        self._sink = sink

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if str(request.tool_call.get("name", "")) != "task":
            return await handler(request)
        args = request.tool_call.get("args", {})
        if not isinstance(args, dict):
            return await handler(request)

        subagent_type = args.get("subagent_type", "")
        description = str(args.get("description", ""))

        if subagent_type != self._target_subagent:
            try:
                return await handler(request)
            except Exception as exc:  # noqa: BLE001
                return _technical_failure(request, subagent_type, exc)

        try:
            browser_result = await handler(request)
        except Exception as exc:  # noqa: BLE001
            return _technical_failure(request, self._target_subagent, exc)
        browser_message = _command_tool_message(browser_result)
        if browser_message is None:
            return browser_result

        snapshot = await self._fresh_snapshot()
        if not snapshot.collected:
            return _technical_failure(request, "PostTaskVerifier", RuntimeError(snapshot.content))
        decision = await self._verify(
            description=description,
            browser_result=str(browser_message.content),
            snapshot=snapshot.content,
        )
        if decision is None:
            return _technical_failure(request, "PostTaskVerifier", RuntimeError("no typed verdict"))
        self._progress.record_verification(
            operation=decision.operation,
            status=decision.status,
            evidence=decision.evidence,
        )
        combined_content = (
            f"BROWSER_SPECIALIST_RESULT:\n{browser_message.content}\n\n"
            "VERIFIER_RESULT:\n"
            f"operation={decision.operation}; status={decision.status}; "
            f"evidence={decision.evidence}"
        )
        combined = ToolMessage(
            content=combined_content,
            tool_call_id=browser_message.tool_call_id,
        )
        base = (
            dict(browser_result.update)
            if isinstance(browser_result, Command) and isinstance(browser_result.update, dict)
            else {}
        )
        return Command(update={**base, "messages": [combined]})

    async def _verify(
        self,
        *,
        description: str,
        browser_result: str,
        snapshot: str,
    ) -> VerificationDecision | None:
        state = _VerdictState()

        @tool
        async def record_verification(
            operation: Literal[
                "form_open",
                "resume_control",
                "resume_upload",
                "field_fill",
                "review_ready",
                "other",
            ],
            status: Literal["verified", "not_verified", "blocked"],
            evidence: str,
        ) -> str:
            """Record the sole evidence-backed result for this browser operation."""
            if state.decision is None:
                state.decision = VerificationDecision(operation, status, evidence)
            return "Typed verification recorded."

        verifier = create_deep_agent(
            model=self._fallback_model,
            tools=[record_verification, *self._tools],
            system_prompt=load_prompt("verifier.md"),
            middleware=[
                NimRouterMiddleware(self._router, role="Verifier"),
                ProseToolCallGuardMiddleware(),
                TerminalDecisionGuardMiddleware(lambda: state.decision is not None),
            ],
        )
        try:
            await consume_deepagent_stream(
                verifier.astream_events(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "Verify this completed browser operation using fresh evidence. "
                                    "Call record_verification exactly once; do not return a prose "
                                    "verdict.\n\n"
                                    f"ORIGINAL OPERATION:\n{description}\n\n"
                                    f"BROWSER RESULT:\n{browser_result}\n\n"
                                    f"FRESH SNAPSHOT:\n{snapshot}"
                                ),
                            }
                        ]
                    },
                    version="v3",
                ),
                sink=self._sink,
                root_source=VERIFIER_SOURCE,
            )
        except TerminalDecisionRecorded:
            pass
        except Exception as exc:  # noqa: BLE001
            _log.warning("Post-task verifier failed: %s", exc)
        return state.decision

    async def _fresh_snapshot(self) -> SnapshotEvidence:
        if self._snapshot_tool is None:
            return SnapshotEvidence(
                "Snapshot unavailable: no browser_snapshot tool was configured.", False
            )
        await self._emit_snapshot("agent_tool_start", {"input": {}})
        try:
            content = str(await self._snapshot_tool.ainvoke({}))
        except Exception as exc:  # noqa: BLE001
            message = f"Snapshot unavailable: {exc}"
            await self._emit_snapshot(
                "agent_tool_end", {"output": "", "error": message, "completed": False}
            )
            return SnapshotEvidence(message, False)
        await self._emit_snapshot(
            "agent_tool_end", {"output": content, "error": "", "completed": True}
        )
        return SnapshotEvidence(content, True)

    async def _emit_snapshot(self, event: str, data: dict[str, Any]) -> None:
        if self._sink is not None:
            await self._sink.accept(
                FrameworkTraceEvent(
                    event, VERIFIER_SOURCE, {"tool_name": "browser_snapshot", **data}, {}
                )
            )


def _command_tool_message(result: ToolMessage | Command[Any]) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result
    if isinstance(result, Command) and isinstance(result.update, dict):
        messages = result.update.get("messages")
        if isinstance(messages, list):
            return next((message for message in messages if isinstance(message, ToolMessage)), None)
    return None


def _technical_failure(request: ToolCallRequest, name: str, error: Exception) -> ToolMessage:
    return ToolMessage(
        content=f"{name} failed before an evidence-backed operation verdict: {error}",
        name="task",
        tool_call_id=str(request.tool_call.get("id", "")),
        status="error",
    )
