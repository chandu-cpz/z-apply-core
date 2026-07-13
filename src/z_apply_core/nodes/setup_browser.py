from __future__ import annotations

import logging
from typing import Any, cast

from langchain_core.runnables.config import RunnableConfig

from z_apply_core.browser_session import BrowserSession
from z_apply_core.browser_tools import INITIAL_AGENT_BROWSER_TOOLS, make_click_upload_tool
from z_apply_core.human.factory import make_configured_human_channel
from z_apply_core.live_view import LiveView
from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.runtime import RunResources, RunRuntime
from z_apply_core.state import RunState
from z_apply_core.virtual_display import VirtualDisplaySession

logger = logging.getLogger(__name__)


async def setup_browser(
    state: RunState,
    config: RunnableConfig,
) -> dict[str, object]:
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
        human_channel = make_configured_human_channel()
        if human_channel is not None:
            bind_run = cast(Any, getattr(human_channel, "bind_run", None))
            if callable(bind_run):
                bind_run(run_id=browser.run_id, url=state["job_url"])
            start = cast(Any, getattr(human_channel, "start", None))
            if callable(start):
                try:
                    await start()
                except Exception as exc:
                    logger.warning(
                        "Telegram human channel listener did not start; "
                        "will retry on first ask_human: %s",
                        exc,
                    )
        runtime = RunRuntime(
            display=display,
            live_view=live_view,
            browser=browser,
            human_channel=human_channel,
            candidate_memory=CandidateMemory(),
            run_id=browser.run_id,
        )
        configurable = config.get("configurable", {})
        resources = configurable.get("run_resources")
        if isinstance(resources, RunResources):
            resources.runtime = runtime
        return {
            "snapshot": snapshot,
            "runtime": runtime,
            "browser_tools": [
                *browser.tools.langchain_tools(INITIAL_AGENT_BROWSER_TOOLS),
                make_click_upload_tool(browser.call_tool),
            ],
        }
    except Exception:
        if browser is not None:
            await browser.close()
        live_view.stop()
        display.stop()
        raise
