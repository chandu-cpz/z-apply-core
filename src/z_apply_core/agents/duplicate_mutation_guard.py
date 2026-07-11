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

from z_apply_core.browser_tools import BROWSER_CHANGING_TOOL_NAMES

_log = logging.getLogger(__name__)


class DuplicateMutationGuardMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Prevent the same browser mutation from being executed twice in one task.

    Tracks completed (tool_name, args) signatures within a single BrowserSpecialist
    semantic task. If the same mutation succeeds once, a second attempt with identical
    arguments is rejected with a guidance message. Failed mutations are forgotten so
    retries remain possible.
    """

    def __init__(self, *, target_subagent: str = "BrowserSpecialist") -> None:
        super().__init__()
        self._target_subagent = target_subagent
        self._completed_mutations: set[tuple[str, str]] = set()

    def _signature(self, tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
        canonical = json.dumps(arguments, sort_keys=True, default=str)
        return (tool_name, canonical)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name = str(request.tool_call.get("name", ""))

        if tool_name == "task":
            arguments = request.tool_call.get("args", {})
            subagent = arguments.get("subagent_type") if isinstance(arguments, dict) else None
            if subagent == self._target_subagent:
                self._completed_mutations.clear()
                _log.info("DuplicateMutationGuard: reset for new %s task", self._target_subagent)
            return await handler(request)

        if tool_name not in BROWSER_CHANGING_TOOL_NAMES:
            return await handler(request)

        arguments = request.tool_call.get("args", {})
        if not isinstance(arguments, dict):
            return await handler(request)

        sig = self._signature(tool_name, arguments)
        if sig in self._completed_mutations:
            _log.info(
                "DuplicateMutationGuard: rejecting duplicate %s with same args",
                tool_name,
            )
            return ToolMessage(
                content=(
                    f"Duplicate mutation prevented: {tool_name} with the same arguments "
                    "already completed successfully in this semantic operation. "
                    "Obtain fresh browser evidence instead of repeating it."
                ),
                name=tool_name,
                tool_call_id=str(request.tool_call.get("id", "")),
            )

        result = await handler(request)
        is_error = getattr(result, "status", "") == "error" or "error" in str(
            getattr(result, "content", "")
        ).lower()
        if not is_error:
            self._completed_mutations.add(sig)
            _log.info(
                "DuplicateMutationGuard: recorded successful %s (total=%d)",
                tool_name,
                len(self._completed_mutations),
            )
        return result
