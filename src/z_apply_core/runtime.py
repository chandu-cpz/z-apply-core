from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from z_apply_core.agents.context_inbox import ContextInbox
from z_apply_core.browser_session import BrowserSession
from z_apply_core.human.channel import HumanChannel
from z_apply_core.live_view import LiveView
from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.teardown import abest_effort, best_effort
from z_apply_core.virtual_display import VirtualDisplaySession


@dataclass(slots=True)
class RunResources:
    """Own resources as soon as setup creates them, including during cancellation."""

    runtime: RunRuntime | None = None


@dataclass(slots=True)
class RunRuntime:
    display: VirtualDisplaySession | None
    live_view: LiveView | None
    browser: BrowserSession
    human_channel: HumanChannel | None = None
    candidate_memory: CandidateMemory | None = None
    run_id: str = ""
    context_inbox: ContextInbox | None = None
    shared_resources: bool = False
    artifact_callback: Callable[[str, Path], Awaitable[None]] | None = None
    # Sync callback the orchestrator's report_job_metadata tool calls with the
    # (company, role, location) it reads off the job page; the service layer
    # binds it to the live run view.
    metadata_reporter: Callable[[str, str, str | None], None] | None = None

    async def close(self) -> None:
        if self.shared_resources:
            return
        if self.human_channel is not None:
            stop = cast(Any, getattr(self.human_channel, "stop", None))
            if callable(stop):
                await abest_effort("RunRuntime human_channel stop", stop)
        if self.candidate_memory is not None:
            best_effort("RunRuntime candidate_memory close", self.candidate_memory.close)
        await abest_effort("RunRuntime browser close", self.browser.close)
        if self.live_view is not None:
            best_effort("RunRuntime live_view stop", self.live_view.stop)
        if self.display is not None:
            best_effort("RunRuntime display stop", self.display.stop)
