from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage
from langgraph.types import Command

_log = logging.getLogger(__name__)

_DELEGATION_ROLES = frozenset({"AnswerWriter"})


class DelegationFailureLadder:
    """Run-scoped counter of failed specialist delegations.

    The escalation design routes candidate-field questions through the
    AnswerWriter instead of letting the orchestrator ask the human directly.
    That deny is correct for the normal path, but it must become conditional
    once the delegate path demonstrably cannot resolve a fact: without this
    ladder an empty-output pathology leaves the orchestrator zero legal moves
    (can't ask, can't invent, delegation returns nothing) and the run stalls
    (observed as FAIL-003). One instance is shared per run between the
    delegation-result middleware that records failures and the orchestrator's
    HumanEscalationGuardMiddleware that consults them.
    """

    def __init__(self, *, threshold: int = 2) -> None:
        self._threshold = threshold
        self._count = 0

    def record_failure(self, subagent: str) -> None:
        _log.warning(
            "delegation ladder: %s failure %d/%d (%s)",
            subagent,
            self._count + 1,
            self._threshold,
            "ladder now OPEN" if self._count + 1 >= self._threshold else "closed",
        )
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    @property
    def tripped(self) -> bool:
        """True once enough delegations failed that direct asking is permitted."""
        return self._count >= self._threshold


class DelegationResultMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Surface empty specialist output as an explicit delegation failure.

    A specialist returning bare emptiness violated its own structured contract;
    handed back verbatim, the empty string gives the orchestrator no signal that
    the delegation failed versus legitimately resolved nothing. Rewriting the
    result to a typed failure both informs the next decision and feeds the
    DelegationFailureLadder that eventually re-opens direct human escalation.
    """

    def __init__(self, ladder: DelegationFailureLadder, *, role: str = "AnswerWriter") -> None:
        super().__init__()
        self._ladder = ladder
        self._role = role

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name = str(request.tool_call.get("name", ""))
        if tool_name != "task":
            return await handler(request)

        arguments = request.tool_call.get("args", {})
        subagent_type = arguments.get("subagent_type") if isinstance(arguments, dict) else None
        result = await handler(request)
        if subagent_type != self._role or not isinstance(result, ToolMessage):
            return result
        if not isinstance(result.content, str) or result.content.strip():
            return result

        self._ladder.record_failure(self._role)
        return ToolMessage(
            content=(
                f"Delegation failed: {self._role} returned no usable output "
                "(empty response against its structured contract). Delegate "
                "once more; if it fails again, call ask_human with reason "
                "missing_candidate_fact — it will be permitted after repeated "
                "delegation failures."
            ),
            name=result.name,
            tool_call_id=result.tool_call_id,
            status="error",
        )
