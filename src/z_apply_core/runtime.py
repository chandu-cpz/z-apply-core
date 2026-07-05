from __future__ import annotations

import contextlib
from dataclasses import dataclass

from z_apply_core.browser_session import BrowserSession
from z_apply_core.live_view import LiveView
from z_apply_core.virtual_display import VirtualDisplaySession


@dataclass(slots=True)
class RunRuntime:
    display: VirtualDisplaySession
    live_view: LiveView
    browser: BrowserSession

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.browser.close()
        self.live_view.stop()
        self.display.stop()
