from __future__ import annotations

from z_apply_core.browser import open_job_with_browser_tools
from z_apply_core.live_view import LiveView
from z_apply_core.state import RunState
from z_apply_core.virtual_display import VirtualDisplaySession


async def setup_browser(state: RunState) -> dict[str, str]:
    display = VirtualDisplaySession(enabled=True)
    live_view = LiveView()
    display.start()
    try:
        live_view.start(display.display, enabled=state.live_view)
        snapshot = await open_job_with_browser_tools(state.job_url)
        return {"snapshot": snapshot}
    finally:
        live_view.stop()
        display.stop()
