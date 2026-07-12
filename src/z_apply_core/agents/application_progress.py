from __future__ import annotations

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
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command

from z_apply_core.agents.application_state import ApplicationState, EvidenceRef, FieldState
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent


@dataclass
class ApplicationProgress:
    """Compatibility view over the typed state ledger used by runtime guards."""

    state: ApplicationState = field(default_factory=ApplicationState)

    @property
    def form_open_verified(self) -> bool:
        return self.state.form_open is not None

    @property
    def resume_control_visible(self) -> bool:
        return self.state.resume_control is not None

    @property
    def resume_uploaded_verified(self) -> bool:
        return self.state.resume_uploaded is not None

    @property
    def fields_mapped(self) -> bool:
        return self.state.fields_mapped

    def update_from_tool_journal(
        self,
        journal: list[dict[str, Any]],
        snapshot: str,
    ) -> None:
        """No-op compatibility hook.

        Browser state is intentionally not inferred from journals or snapshot text.
        Typed specialist/verifier tools commit transitions at their own boundaries.
        """
        del journal, snapshot

    def mark_fields_mapped(self) -> None:
        if not self.state.fields:
            raise ValueError("Field mapping requires typed field records.")

    def mark_form_open(self) -> None:
        self.state.form_open = EvidenceRef("verifier", "runtime", "explicit form-open verdict")

    def record_verification(
        self,
        *,
        operation: str,
        status: str,
        evidence: str,
    ) -> None:
        if status != "verified":
            return
        ref = EvidenceRef("verifier", "PostTaskVerifier", evidence)
        if operation == "form_open":
            self.state.form_open = ref
        elif operation == "resume_control":
            self.state.resume_control = ref
        elif operation == "resume_upload":
            self.state.resume_uploaded = ref
        elif operation == "review_ready":
            self.state.review_complete = ref
        elif operation == "field_fill":
            self.state.filled_fields["verified field batch"] = ref

    def record_human_answer(self, field_label: str) -> None:
        self.state.human_answers[field_label] = EvidenceRef(
            "human", "ask_human", "human answer recorded"
        )

    def record_submit_approval(self, approved: bool) -> None:
        self.state.approval_requested = EvidenceRef(
            "human", "request_submit_approval", "human decision recorded"
        )
        self.state.approval_status = "approved" if approved else "rejected"


def make_field_map_tools(progress: ApplicationProgress) -> list[BaseTool]:
    @tool
    async def record_field_map(
        fields: list[dict[str, object]],
        resume_control_visible: bool,
        evidence: str,
    ) -> str:
        """Record the complete current field map from fresh read-only browser evidence."""
        mapped: dict[str, FieldState] = {}
        allowed = {
            "already_satisfied",
            "candidate_fact_available",
            "human_answer_needed",
            "ambiguous",
            "deferred_challenge",
        }
        for item in fields:
            label, ref, required, status = (
                item.get("label"),
                item.get("ref"),
                item.get("required"),
                item.get("status"),
            )
            if not isinstance(label, str) or not label.strip() or not isinstance(ref, str):
                raise ValueError("Each mapped field needs non-empty label and ref.")
            if not isinstance(required, bool) or status not in allowed:
                raise ValueError("Each mapped field needs boolean required and valid status.")
            mapped[label] = FieldState(
                label=label,
                ref=ref,
                required=required,
                status=status,  # type: ignore[arg-type]
                evidence=EvidenceRef("field_map", "FieldMapper", evidence),
            )
        progress.state.fields = mapped
        if resume_control_visible:
            progress.state.resume_control = EvidenceRef("field_map", "FieldMapper", evidence)
        return "Typed field map recorded."

    return [record_field_map]


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
        if self._delegate is not None:
            await self._delegate.accept(event)


class BrowserUploadProgressMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Compatibility pass-through; upload requires a typed verifier verdict."""

    def __init__(self, progress: ApplicationProgress) -> None:
        super().__init__()
        self._progress = progress

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        return result
