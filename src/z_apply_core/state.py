from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool
from typing_extensions import TypedDict

from z_apply_core.runtime import RunRuntime


class RunState(TypedDict, total=False):
    job_url: str
    live_view: bool
    snapshot: str
    status: str
    reason: str
    runtime: RunRuntime | None
    browser_tools: Sequence[BaseTool]
    messages: list[Any]
    structured_response: Any


def initial_state(job_url: str, *, live_view: bool) -> RunState:
    return {
        "job_url": job_url,
        "live_view": live_view,
        "snapshot": "",
        "status": "",
        "reason": "",
        "runtime": None,
        "browser_tools": (),
    }
