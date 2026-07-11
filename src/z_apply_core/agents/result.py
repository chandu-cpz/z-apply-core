from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RunStatus = Literal["not_started", "completed", "incomplete", "failed"]
AuthStatus = Literal["authenticated", "blocked", "not_verified", "failed"]


@dataclass(frozen=True, slots=True)
class OrchestratorRun:
    summary: str
    model_id: str
    status: RunStatus = "completed"


@dataclass(frozen=True, slots=True)
class AuthOrchestratorRun:
    summary: str
    model_id: str
    status: AuthStatus
