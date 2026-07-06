from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OrchestratorRun:
    summary: str
    model_id: str
