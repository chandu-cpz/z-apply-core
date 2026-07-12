from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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

from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

_log = logging.getLogger(__name__)


@dataclass
class ApplicationProgress:
    """Minimal runtime-tracked application state from actual browser/verifier evidence."""

    form_open_verified: bool = False
    resume_control_visible: bool = False
    resume_uploaded_verified: bool = False
    fields_mapped: bool = False
    _resume_control_seen_in_snapshot: bool = field(default=False, repr=False)

    def update_from_tool_journal(
        self,
        journal: list[dict[str, Any]],
        snapshot: str,
    ) -> None:
        """Derive progress from actual tool trace and browser evidence."""
        snapshot_lower = snapshot.lower()

        upload_verified = False
        form_open = False
        for entry in journal:
            tool_name = entry.get("tool_name", "")
            tool_input = entry.get("input", {})
            output = str(entry.get("output", ""))
            completed = entry.get("completed") and not entry.get("error")

            if tool_name == "browser_file_upload" and completed:
                upload_verified = True

            if (
                tool_name == "task"
                and completed
                and isinstance(tool_input, dict)
                and tool_input.get("subagent_type") == "FieldMapper"
            ):
                self.fields_mapped = True

            if tool_name == "browser_click" and completed:
                output_lower = output.lower()
                if "snapshot" in output_lower:
                    form_open = True

        self.resume_uploaded_verified = self.resume_uploaded_verified or upload_verified

        if form_open and not self.form_open_verified:
            self.form_open_verified = True

        resume_keywords = (
            "resume",
            "upload",
            "cv",
            "choose file",
            "file input",
            "browse",
        )
        self.resume_control_visible = any(kw in snapshot_lower for kw in resume_keywords)

    def mark_fields_mapped(self) -> None:
        self.fields_mapped = True

    def mark_form_open(self) -> None:
        self.form_open_verified = True


class ApplicationProgressEventSink:
    """Track typed nested tool completion events while preserving UI streaming."""

    def __init__(
        self,
        progress: ApplicationProgress,
        delegate: FrameworkEventSink | None = None,
    ) -> None:
        self._progress = progress
        self._delegate = delegate

    async def accept(self, event: FrameworkTraceEvent) -> None:
        if (
            event.event == "agent_tool_end"
            and event.data.get("tool_name") == "browser_file_upload"
            and event.data.get("completed") is True
            and not event.data.get("error")
        ):
            self._progress.resume_uploaded_verified = True

        if self._delegate is not None:
            await self._delegate.accept(event)


class BrowserUploadProgressMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Commit completed upload state at the nested browser-tool boundary."""

    def __init__(self, progress: ApplicationProgress) -> None:
        super().__init__()
        self._progress = progress

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if (
            str(request.tool_call.get("name", "")) == "browser_file_upload"
            and not _tool_result_is_error(result)
        ):
            self._progress.resume_uploaded_verified = True
        return result


def _tool_result_is_error(result: ToolMessage | Command[Any]) -> bool:
    return isinstance(result, ToolMessage) and result.status == "error"
