from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables.config import RunnableConfig

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.stream_events import FrameworkEventSink

logger = logging.getLogger(__name__)

MAX_GOAL_TURNS = 100


class ActiveGoalExhausted(RuntimeError):
    """The persistent goal remained active after too many completed agent turns."""


async def run_active_goal(
    agent: Any,
    *,
    initial_message: str,
    config: RunnableConfig,
    sink: FrameworkEventSink | None,
    is_terminal: Callable[[], bool],
    source: str = "orchestrator",
) -> None:
    """Continue one checkpointed agent thread until a typed terminal action."""
    turn_input: dict[str, Any] = {
        "messages": [{"role": "user", "content": initial_message}]
    }
    for turn in range(1, MAX_GOAL_TURNS + 1):
        await consume_deepagent_stream(
            agent.astream_events(
                cast(Any, turn_input),
                config=config,
                version="v3",
            ),
            sink=sink,
            root_source=source,
        )
        if is_terminal():
            return
        logger.info("Active goal continuing after agent turn %s", turn)
        turn_input = {
            "messages": [
                HumanMessage(
                    content=(
                        "The job-application goal is still active. Continue from the same "
                        "thread, todos, browser state, and prior tool results. Do not repeat "
                        "completed actions. If the latest results are AnswerWriter FIELD / "
                        "VALUE pairs, your next actions must apply those exact values to "
                        "their browser refs; do not request them again. Otherwise choose "
                        "the next safe browser action. The goal ends only through "
                        "application_submitted or application_blocked."
                    )
                )
            ]
        }
    raise ActiveGoalExhausted(
        f"Active job-application goal exceeded {MAX_GOAL_TURNS} agent turns."
    )
