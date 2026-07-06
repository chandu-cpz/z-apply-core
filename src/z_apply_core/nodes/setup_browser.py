from __future__ import annotations

from z_apply_core.browser_session import BrowserSession
from z_apply_core.browser_tools import INITIAL_AGENT_BROWSER_TOOLS
from z_apply_core.live_view import LiveView
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState
from z_apply_core.virtual_display import VirtualDisplaySession


async def setup_browser(state: RunState) -> dict[str, object]:
    display = VirtualDisplaySession(enabled=True)
    live_view = LiveView()
    browser: BrowserSession | None = None
    display.start()
    try:
        live_view.start(display.display, enabled=bool(state.get("live_view", True)))
        browser = await BrowserSession.start()
        snapshot = await browser.tools.call("browser_navigate", {"url": state["job_url"]})
        if not snapshot.startswith("### Error"):
            snapshot = await browser.tools.call("browser_snapshot")
        runtime = RunRuntime(display=display, live_view=live_view, browser=browser)
        return {
            "snapshot": snapshot,
            "runtime": runtime,
            "browser_tools": browser.tools.langchain_tools(INITIAL_AGENT_BROWSER_TOOLS),
        }
    except Exception:
        if browser is not None:
            await browser.close()
        live_view.stop()
        display.stop()
        raise
