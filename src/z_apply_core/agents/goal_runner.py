from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ResponseT,
    ToolCallRequest,
    hook_config,
)
from langchain_core.messages import HumanMessage
from langchain_core.messages.tool import ToolMessage
from langchain_core.runnables.config import RunnableConfig
from langgraph.types import Command

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.browser_tools import BROWSER_CHANGING_TOOL_NAMES
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

logger = logging.getLogger(__name__)

ACTIVE_OBJECTIVE_SOURCE = "active_objective_controller"
MAX_NO_PROGRESS_RECOVERIES = 20
MAX_GOAL_RUN_RECOVERIES = 100

_PROGRESS_TOOL_NAMES = BROWSER_CHANGING_TOOL_NAMES | {"task", "ask_human"}


class ActiveGoalExhausted(RuntimeError):
    """The persistent goal repeatedly stopped without a terminal action."""


def _tool_succeeded(result: ToolMessage | Command[Any]) -> bool:
    return not isinstance(result, ToolMessage) or result.status != "error"


class ActiveGoalMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Keep an unfinished objective inside one DeepAgents graph invocation.

    A normal LangChain agent ends when a model returns prose without tool calls.
    That is never a valid state for the active job-application objective. This
    middleware turns the natural stop into source-tagged controller feedback and
    jumps directly back to the model while preserving the graph state.
    """

    def __init__(
        self,
        *,
        is_terminal: Callable[[], bool],
        on_no_progress: Callable[[ToolProtocolViolation], None],
        max_recoveries: int = MAX_NO_PROGRESS_RECOVERIES,
    ) -> None:
        super().__init__()
        self._is_terminal = is_terminal
        self._on_no_progress = on_no_progress
        self._max_recoveries = max_recoveries
        self._recoveries = 0

    @hook_config(can_jump_to=["model"])
    def after_agent(
        self,
        state: AgentState[ResponseT],
        runtime: Any,
    ) -> dict[str, Any] | None:
        del state, runtime
        return self._continue_or_finish()

    async def aafter_agent(
        self,
        state: AgentState[ResponseT],
        runtime: Any,
    ) -> dict[str, Any] | None:
        del state, runtime
        return self._continue_or_finish()

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest], Awaitable[ToolMessage | Command[Any]]
        ],
    ) -> ToolMessage | Command[Any]:
        """Credit progress only after a progress-capable tool succeeds.

        Read-only inspection is useful evidence, but it must not erase a run of
        prose-only stops. Crediting the proposed tool call in ``after_model``
        allowed an endless snapshot -> prose -> snapshot loop. The executor
        result is the first boundary where success is known.
        """
        result = await handler(request)
        tool_name = str(request.tool_call.get("name", ""))
        if tool_name in _PROGRESS_TOOL_NAMES and _tool_succeeded(result):
            self._recoveries = 0
        return result

    def _continue_or_finish(self) -> dict[str, Any] | None:
        if self._is_terminal():
            return None

        self._recoveries += 1
        if self._recoveries > self._max_recoveries:
            raise ActiveGoalExhausted(
                "Active job-application goal repeatedly ended without a native action."
            )

        failure = ToolProtocolViolation(
            "tool_protocol_failure: model ended an active job-application objective "
            "without a native tool call or typed terminal action"
        )
        self._on_no_progress(failure)
        logger.warning(
            "Active objective rejected prose-only stop; continuing in-graph (recovery=%s/%s)",
            self._recoveries,
            self._max_recoveries,
        )
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "ACTIVE OBJECTIVE CONTROLLER: the application is still active, "
                        "so your prose response did not advance or finish it. Continue "
                        "from the current browser state and newest tool results. Emit "
                        "exactly one next native tool action now. Use application_submitted "
                        "or application_blocked only when its evidence condition is true."
                    ),
                    name=ACTIVE_OBJECTIVE_SOURCE,
                    additional_kwargs={"lc_source": ACTIVE_OBJECTIVE_SOURCE},
                )
            ],
            "jump_to": "model",
        }


async def run_active_goal(
    agent: Any,
    *,
    initial_message: str,
    config: RunnableConfig,
    sink: FrameworkEventSink | None,
    source: str = "orchestrator",
) -> None:
    """Stream one graph invocation; ActiveGoalMiddleware owns continuation."""
    await consume_deepagent_stream(
        agent.astream_events(
            cast(Any, {"messages": [{"role": "user", "content": initial_message}]}),
            config=config,
            version="v3",
        ),
        sink=sink,
        root_source=source,
    )


async def run_persistent_goal(
    agent: Any,
    *,
    initial_message: str,
    config: RunnableConfig,
    sink: FrameworkEventSink | None,
    is_terminal: Callable[[], bool],
    source: str = "orchestrator",
    max_recoveries: int = MAX_GOAL_RUN_RECOVERIES,
    recovery_delay_seconds: float = 1.0,
) -> None:
    """Re-enter one checkpointed goal thread after exhausted model attempts."""
    message = initial_message
    recovered = 0
    while True:
        try:
            await run_active_goal(
                agent,
                initial_message=message,
                config=config,
                sink=sink,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001 - recovery owns model/provider failures
            if is_terminal():
                return
            if recovered >= max_recoveries:
                await _emit_recovery(
                    sink,
                    "recovery_exhausted",
                    source,
                    recovered,
                    exc,
                )
                raise
            recovered += 1
            await _emit_recovery(sink, "recovery_started", source, recovered, exc)
            if recovery_delay_seconds > 0:
                await asyncio.sleep(recovery_delay_seconds)
            message = (
                "RUN GOAL CONTROLLER: the active application goal survived a model "
                "or provider failure. Continue in this same checkpointed thread. "
                "Preserve completed browser actions and newest live user context. "
                "Capture fresh browser evidence, choose exactly one safe next action, "
                "and emit it through the native tool-call channel. Do not repeat a "
                "mutation against unchanged evidence.\n\n"
                f"Recovery attempt: {recovered}/{max_recoveries}\n"
                f"Previous failure type: {type(exc).__name__}\n"
                f"Previous failure: {str(exc) or 'provider call ended without details'}"
            )
            continue
        if recovered:
            await _emit_recovery(sink, "recovery_completed", source, recovered, None)
        return


async def _emit_recovery(
    sink: FrameworkEventSink | None,
    event: str,
    source: str,
    attempt: int,
    error: Exception | None,
) -> None:
    if sink is None:
        return
    data: dict[str, Any] = {"attempt": attempt}
    if error is not None:
        data.update(
            {
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
    await sink.accept(
        FrameworkTraceEvent(
            event=event,
            name=source,
            data=data,
            raw={},
        )
    )
