from __future__ import annotations

import json
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

from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.browser_tools import BROWSER_CHANGING_TOOL_NAMES

_PROGRESS_TOOL_NAMES = BROWSER_CHANGING_TOOL_NAMES | {
    "application_blocked",
    "application_submitted",
    "ask_human",
    "request_submit_approval",
    "task",
}
_REPEATABLE_READ_TOOL_NAMES = frozenset({"browser_wait_for"})


class NoProgressCircuitOpen(RuntimeError):
    """The active agent repeatedly attempted calls that cannot make progress."""


class NoProgressGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """End one agent turn after repeated denied or duplicate non-progress calls."""

    def __init__(
        self,
        *,
        max_identical_denials: int = 2,
        max_non_progress: int = 3,
        on_no_progress: Callable[[ToolProtocolViolation], None] | None = None,
    ) -> None:
        super().__init__()
        self._max_identical_denials = max_identical_denials
        self._max_non_progress = max_non_progress
        self._last_denial = ""
        self._same_denials = 0
        self._non_progress = 0
        self._on_no_progress = on_no_progress
        self._last_read_signature: str | None = None

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        read_signature = _read_signature(request)
        if read_signature is not None and read_signature == self._last_read_signature:
            result = ToolMessage(
                content=(
                    "RUNTIME NO-PROGRESS: this exact read-only tool call already "
                    "succeeded against unchanged state. Reuse its result and choose "
                    "a different action."
                ),
                name=str(request.tool_call.get("name", "runtime")),
                tool_call_id=str(request.tool_call.get("id", "")),
                status="error",
            )
        else:
            result = await handler(request)

        tool_name = str(request.tool_call.get("name", ""))
        if _tool_succeeded(result):
            if tool_name in _PROGRESS_TOOL_NAMES:
                self._last_read_signature = None
            elif read_signature is not None:
                self._last_read_signature = read_signature
        if _is_non_progress(result):
            detail = str(getattr(result, "content", ""))
            self._non_progress += 1
            self._same_denials = self._same_denials + 1 if detail == self._last_denial else 1
            self._last_denial = detail
            if (
                self._same_denials >= self._max_identical_denials
                or self._non_progress >= self._max_non_progress
            ):
                failure = ToolProtocolViolation(
                    "no_progress: repeated denied or non-progress tool calls require a "
                    "different model and action"
                )
                if self._on_no_progress is not None:
                    self._on_no_progress(failure)
                self._last_denial = ""
                self._same_denials = 0
                self._non_progress = 0
                return ToolMessage(
                    content=(
                        "RUNTIME NO-PROGRESS RECOVERY: the repeated action was denied and "
                        "the active model was rotated. Do not retry it. Use the newest "
                        "browser evidence and choose a different authorized tool action."
                    ),
                    name=str(request.tool_call.get("name", "runtime")),
                    tool_call_id=str(request.tool_call.get("id", "")),
                    status="error",
                )
        else:
            self._last_denial = ""
            self._same_denials = 0
            self._non_progress = 0
        return result


def _is_non_progress(result: ToolMessage | Command[Any]) -> bool:
    if not isinstance(result, ToolMessage):
        return False
    text = str(result.content).lower()
    return result.status == "error" or "denied:" in text or "duplicate mutation prevented" in text


def _read_signature(request: ToolCallRequest) -> str | None:
    name = str(request.tool_call.get("name", ""))
    if name in _PROGRESS_TOOL_NAMES or name in _REPEATABLE_READ_TOOL_NAMES:
        return None
    args = request.tool_call.get("args", {})
    return f"{name}:{json.dumps(args, sort_keys=True, default=str, separators=(',', ':'))}"


def _tool_succeeded(result: ToolMessage | Command[Any]) -> bool:
    return not isinstance(result, ToolMessage) or result.status != "error"
