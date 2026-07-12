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
    a structured ``SPECIALIST_FAILURE`` ToolMessage to the parent Orchestrator
    so the Orchestrator itself decides recovery.

    The only proof of completion is the ``field_map_commits`` counter
    incrementing.  Agent prose is never authoritative truth.
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
        call_id = str(request.tool_call.get("id", ""))

        try:
            result = await handler(request)
        except Exception as exc:  # noqa: BLE001
            if self._progress.field_map_commits > commits_before:
                _log.warning(
                    "SpecialistContract: FieldMapper committed typed state "
                    "then crashed: %s; preserving committed result",
                    exc,
                )
                return _specialist_failure(
                    kind="specialist_exception_after_commit",
                    committed=True,
                    detail=str(exc),
                    call_id=call_id,
                )
            return _specialist_failure(
                kind="specialist_exception",
                committed=False,
                detail=str(exc),
                call_id=call_id,
            )

        if self._progress.field_map_commits > commits_before:
            return result

        _log.warning(
            "SpecialistContract: FieldMapper task finished without calling "
            "record_field_map; returning SPECIALIST_FAILURE to Orchestrator"
        )
        return _specialist_failure(
            kind="required_tool_missing",
            committed=False,
            detail="FieldMapper finished without calling record_field_map",
            call_id=call_id,
        )


def _specialist_failure(
    *,
    kind: str,
    committed: bool,
    detail: str,
    call_id: str,
) -> ToolMessage:
    payload = {
        "role": "FieldMapper",
        "kind": kind,
        "required_tool": "record_field_map",
        "committed": committed,
        "detail": detail,
        "recovery_owner": "Orchestrator",
    }
    return ToolMessage(
        content=f"SPECIALIST_FAILURE:\n{json.dumps(payload, indent=2)}",
        name="task",
        tool_call_id=call_id,
    )
