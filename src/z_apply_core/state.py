from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool

from z_apply_core.runtime import RunRuntime


@dataclass(slots=True)
class RunState:
    job_url: str
    live_view: bool = True
    snapshot: str = ""
    runtime: RunRuntime | None = None
    browser_tools: Sequence[BaseTool] = field(default_factory=tuple)
