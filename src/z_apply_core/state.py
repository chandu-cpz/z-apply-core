from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool
from typing_extensions import TypedDict

from z_apply_core.runtime import RunRuntime


class RunState(TypedDict, total=False):
    job_url: str
    task: str
    live_view: bool
    snapshot: str
    orchestrator_summary: str
    model_id: str
    runtime: RunRuntime | None
    browser_tools: Sequence[BaseTool]
    messages: list[Any]


def initial_state(job_url: str, *, task: str, live_view: bool) -> RunState:
    return {
        "job_url": job_url,
        "task": task,
        "live_view": live_view,
        "snapshot": "",
        "orchestrator_summary": "",
        "model_id": "",
        "runtime": None,
        "browser_tools": (),
    }
