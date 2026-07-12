from __future__ import annotations

import json
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


class SpecialistCompletionContractMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Enforce required tool-execution contracts for specialist tasks.

    When a native ``task`` call targets a specialist with required tools,
    this middleware verifies the contract was satisfied.  If not, it returns
    a real ``ToolMessage`` to the parent Orchestrator so the Orchestrator
    itself decides recovery.
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
        if tool_name != "task":
            return await handler(request)

        args = request.tool_call.get("args", {})
        if not isinstance(args, dict):
            return await handler(request)

        subagent_type = args.get("subagent_type", "")
        if subagent_type != "FieldMapper":
            return await handler(request)

        commits_before = self._progress.field_map_commits

        try:
            result = await handler(request)
        except Exception as exc:  # noqa: BLE001
            return ToolMessage(
                content=(
                    f"FieldMapper task failed before completing required work: {exc}"
                ),
                name="task",
                tool_call_id=str(request.tool_call.get("id", "")),
                status="error",
            )

        if self._progress.field_map_commits > commits_before:
            return result

        if _has_committed_typed_state(result):
            _log.info(
                "SpecialistContract: FieldMapper committed typed state despite "
                "counter not incrementing; preserving result"
            )
            return result

        _log.warning(
            "SpecialistContract: FieldMapper task finished without calling "
            "record_field_map; returning SPECIALIST_FAILURE to Orchestrator"
        )
        failure_payload = {
            "role": "FieldMapper",
            "kind": "required_tool_missing",
            "required_tool": "record_field_map",
            "committed": False,
            "recovery_owner": "Orchestrator",
        }
        return ToolMessage(
            content=(
                f"SPECIALIST_FAILURE:\n{json.dumps(failure_payload, indent=2)}"
            ),
            name="task",
            tool_call_id=str(request.tool_call.get("id", "")),
        )


def _has_committed_typed_state(result: ToolMessage | Command[Any]) -> bool:
    """Return True if the result carries evidence of committed typed state."""
    text = ""
    if isinstance(result, ToolMessage):
        text = str(result.content)
    elif isinstance(result, Command) and isinstance(result.update, dict):
        messages = result.update.get("messages", [])
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, ToolMessage):
                    text = str(msg.content)
                    break
    return "Typed field map recorded" in text or "BROWSER_SPECIALIST_RESULT" in text
