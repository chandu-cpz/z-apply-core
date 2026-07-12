from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, cast

from z_apply_core.browser_session import BrowserSession
from z_apply_core.human.channel import HumanChannel
from z_apply_core.live_view import LiveView
from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.virtual_display import VirtualDisplaySession


@dataclass(slots=True)
class RunRuntime:
    display: VirtualDisplaySession
    live_view: LiveView
    browser: BrowserSession
    human_channel: HumanChannel | None = None
    candidate_memory: CandidateMemory | None = None
    run_id: str = ""

    async def close(self) -> None:
        if self.human_channel is not None:
            with contextlib.suppress(Exception):
                stop = cast(Any, getattr(self.human_channel, "stop", None))
                if callable(stop):
                    await stop()
        if self.candidate_memory is not None:
            with contextlib.suppress(Exception):
                self.candidate_memory.close()
        with contextlib.suppress(Exception):
            await self.browser.close()
        self.live_view.stop()
        self.display.stop()
