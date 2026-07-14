"""Transport-neutral, JSON-safe types exposed by Z-Apply Core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    HUMAN_CONTROL = "human_control"
    TERMINAL = "terminal"


class RunPhase(StrEnum):
    QUEUED = "queued"
    SETUP = "setup"
    AUTHENTICATION = "authentication"
    APPLICATION = "application"
    REVIEW = "review"
    APPROVAL = "approval"
    SUBMISSION = "submission"
    VERIFICATION = "verification"
    TERMINAL = "terminal"


class RunOutcome(StrEnum):
    SUBMITTED_VERIFIED = "submitted_verified"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class BrowserControlMode(StrEnum):
    AGENT_CONTROL = "agent_control"
    HUMAN_CONTROL = "human_control"


class BrowserTabState(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class CoreIntegrationConfig:
    max_active_runs: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_active_runs <= 8:
            raise ValueError("max_active_runs must be from 1 through 8")


@dataclass(frozen=True, slots=True)
class StartRunRequest:
    job_url: str
    task: str | None = None
    resume_path: Path | None = None
    live_view: bool = True


@dataclass(frozen=True, slots=True)
class CoreContextMessage:
    run_id: str
    content: str
    source: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class CoreEvent:
    run_id: str
    sequence: int
    occurred_at: datetime
    type: str
    source: Mapping[str, str] = field(default_factory=lambda: {"component": "core"})
    level: str = "info"
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        return data


@dataclass(frozen=True, slots=True)
class CoreArtifact:
    artifact_id: str
    run_id: str
    kind: str
    filename: str
    mime_type: str
    relative_path: str
    size_bytes: int
    sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CoreHumanRequest:
    request_id: str
    run_id: str
    kind: str
    question: str
    context: str
    options: tuple[str, ...]
    risk: str
    allow_free_text: bool
    image_artifact_id: str | None
    created_at: datetime
    status: str = "pending"
    answer: str | None = None
    approved: bool | None = None
    responder: str | None = None
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CoreRunView:
    run_id: str
    job_url: str
    task: str | None
    company: str | None
    role: str | None
    status: RunStatus
    phase: RunPhase
    outcome: RunOutcome | None
    summary: str | None
    current_agent: str | None
    current_model: str | None
    browser_tab_state: BrowserTabState
    control_mode: BrowserControlMode
    pending_human_request_id: str | None
    latest_event_sequence: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class CoreRunResult:
    run_id: str
    outcome: RunOutcome
    summary: str
    finished_at: datetime
    event_count: int


@dataclass(frozen=True, slots=True)
class CoreLiveView:
    available: bool
    vnc_host: str | None
    vnc_port: int | None
    control_mode: BrowserControlMode
    focused_run_id: str | None
