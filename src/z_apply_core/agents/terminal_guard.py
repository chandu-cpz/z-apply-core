from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ContextT, ResponseT


class TerminalDecisionRecorded(RuntimeError):
    """A controller recorded its sole terminal decision and must not generate again."""


class TerminalDecisionGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Stop the next model turn after an external typed terminal state transition."""

    def __init__(self, recorded: Callable[[], bool]) -> None:
        super().__init__()
        self._recorded = recorded

    async def abefore_model(
        self, state: AgentState[ResponseT], runtime: Any
    ) -> dict[str, Any] | None:
        if self._recorded():
            raise TerminalDecisionRecorded("terminal_decision_recorded")
        return None
