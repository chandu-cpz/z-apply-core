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

from z_apply_core.agents.application_progress import ApplicationProgress

_log = logging.getLogger(__name__)

_HUMAN_CHALLENGE_REASONS = frozenset({"human_challenge"})
_VALID_REASONS = frozenset({"missing_candidate_fact", "ambiguous_field", "human_challenge"})


class HumanEscalationGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Prevent premature or unjustified ask_human calls.

    Enforces typed reasons and preconditions before allowing ask_human.
    request_submit_approval is not affected.
    """

    def __init__(self, progress: ApplicationProgress) -> None:
        super().__init__()
        self._progress = progress

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

        if self._progress.resume_control_visible and not self._progress.resume_uploaded_verified:
            _log.info("HumanEscalationGuard: rejecting ask_human - resume upload pending")
            return ToolMessage(
                content=(
                    "Human escalation denied: independent automation work remains. "
                    "The configured resume has not been verified as uploaded and a primary "
                    "resume control is available. Complete and verify resume upload first, "
                    "then map the resulting fields before asking the human for missing information."
                ),
                name="ask_human",
                tool_call_id=str(request.tool_call.get("id", "")),
            )

        if not self._progress.fields_mapped:
            _log.info("HumanEscalationGuard: rejecting ask_human - fields not mapped yet")
            return ToolMessage(
                content=(
                    "Human escalation denied: fields have not been mapped yet. "
                    "Call FieldMapper to map visible fields before asking for missing information."
                ),
                name="ask_human",
                tool_call_id=str(request.tool_call.get("id", "")),
            )

        return await handler(request)
