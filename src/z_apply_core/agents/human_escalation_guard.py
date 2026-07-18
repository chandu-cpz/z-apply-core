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

_HUMAN_CHALLENGE_REASONS = frozenset({"human_challenge"})
_VALID_REASONS = frozenset({"missing_candidate_fact", "ambiguous_field", "human_challenge"})


class HumanEscalationGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Prevent premature or unjustified ask_human calls.

    Enforces typed reasons and preconditions before allowing ask_human.
    request_submit_approval is not affected.
    """

    def __init__(
        self,
        _legacy_progress: object | None = None,
        *,
        allowed_reasons: frozenset[str] | None = None,
        required_prior_tools: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self._allowed_reasons = allowed_reasons or _VALID_REASONS
        self._required_prior_tools = required_prior_tools

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name = str(request.tool_call.get("name", ""))

        if tool_name != "ask_human":
            return await handler(request)

        arguments = request.tool_call.get("args", {})
        if not isinstance(arguments, dict):
            return await handler(request)

        reason = arguments.get("reason", "")
        if not isinstance(reason, str) or reason not in _VALID_REASONS:
            _log.info(
                "HumanEscalationGuard: rejecting ask_human with invalid reason=%r",
                reason,
            )
            return ToolMessage(
                content=(
                    "Human escalation denied: invalid reason. "
                    "Use one of: missing_candidate_fact, ambiguous_field, human_challenge. "
                    "Provide reason as a typed keyword and specific field evidence."
                ),
                name="ask_human",
                tool_call_id=str(request.tool_call.get("id", "")),
            )

        if reason not in self._allowed_reasons:
            allowed = ", ".join(sorted(self._allowed_reasons))
            _log.info(
                "HumanEscalationGuard: rejecting ask_human reason=%r for this agent",
                reason,
            )
            return ToolMessage(
                content=(
                    "Human escalation denied for this agent. "
                    f"Allowed reason here: {allowed}. Delegate candidate-field questions "
                    "to AnswerWriter."
                ),
                name="ask_human",
                tool_call_id=str(request.tool_call.get("id", "")),
            )

        if reason in _HUMAN_CHALLENGE_REASONS:
            return await handler(request)

        field_label = arguments.get("field_label", "")
        arguments.get("field_evidence", "")

        if not isinstance(field_label, str) or not field_label.strip():
            _log.info("HumanEscalationGuard: rejecting ask_human with no field_label")
            return ToolMessage(
                content=(
                    "Human escalation denied: no specific field identified. "
                    "Provide field_label naming the exact required field(s)."
                ),
                name="ask_human",
                tool_call_id=str(request.tool_call.get("id", "")),
            )

        completed_tools = {
            message.name
            for message in request.state.get("messages", [])
            if isinstance(message, ToolMessage) and message.name
        }
        missing_tools = self._required_prior_tools - completed_tools
        if missing_tools:
            required = ", ".join(sorted(missing_tools))
            return ToolMessage(
                content=(
                    "Human escalation denied: candidate evidence is not exhausted. "
                    f"Complete these evidence tools before asking the human: {required}."
                ),
                name="ask_human",
                tool_call_id=str(request.tool_call.get("id", "")),
            )

        return await handler(request)
