from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OrchestratorResult(BaseModel):
    status: Literal["success", "blocked", "failed"]
    reason: str = Field(description="Short human-readable reason for the terminal status.")
