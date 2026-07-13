from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ResponseT,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables.config import RunnableConfig

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.stream_events import FrameworkEventSink

logger = logging.getLogger(__name__)

ACTIVE_OBJECTIVE_SOURCE = "active_objective_controller"
MAX_NO_PROGRESS_RECOVERIES = 20


class ActiveGoalExhausted(RuntimeError):
    """The persistent goal repeatedly stopped without a terminal action."""


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

    def after_model(
        self,
        state: AgentState[ResponseT],
        runtime: Any,
    ) -> None:
        del runtime
        self._reset_after_native_action(state)

    async def aafter_model(
        self,
        state: AgentState[ResponseT],
        runtime: Any,
    ) -> None:
        del runtime
        self._reset_after_native_action(state)

    def _reset_after_native_action(self, state: AgentState[ResponseT]) -> None:
        messages = state.get("messages", ())
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            self._recoveries = 0

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
            "Active objective rejected prose-only stop; continuing in-graph "
            "(recovery=%s/%s)",
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
