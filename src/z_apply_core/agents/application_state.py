from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EvidenceKind = Literal["browser", "field_map", "human", "verifier"]


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A claim's concrete source; assistant prose is never an evidence source."""

    kind: EvidenceKind
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class FieldState:
    label: str
    ref: str
    required: bool
    status: Literal[
        "already_satisfied",
        "candidate_fact_available",
        "human_answer_needed",
        "ambiguous",
        "deferred_challenge",
    ]
    evidence: EvidenceRef


@dataclass(slots=True)
class ApplicationState:
    """Authoritative application facts collected at typed runtime boundaries."""

    form_open: EvidenceRef | None = None
    resume_control: EvidenceRef | None = None
    resume_uploaded: EvidenceRef | None = None
    fields: dict[str, FieldState] = field(default_factory=dict)
    filled_fields: dict[str, EvidenceRef] = field(default_factory=dict)
    human_answers: dict[str, EvidenceRef] = field(default_factory=dict)
    review_complete: EvidenceRef | None = None
    approval_requested: EvidenceRef | None = None
    approval_status: Literal["approved", "rejected"] | None = None

    @property
    def fields_mapped(self) -> bool:
        return bool(self.fields)

    @property
    def unresolved_required(self) -> tuple[FieldState, ...]:
        return tuple(
            item
            for item in self.fields.values()
            if item.required and item.status in {"human_answer_needed", "ambiguous"}
        )
