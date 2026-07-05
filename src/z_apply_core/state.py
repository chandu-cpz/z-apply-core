from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RunState:
    job_url: str
    live_view: bool = True
    snapshot: str = ""
