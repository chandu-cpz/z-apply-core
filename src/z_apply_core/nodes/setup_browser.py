from __future__ import annotations

import logging
from typing import Any, cast

from langchain_core.runnables.config import RunnableConfig

from z_apply_core.agents.context_inbox import ContextInbox
from z_apply_core.browser_session import BrowserSession
from z_apply_core.browser_tools import (
    INITIAL_AGENT_BROWSER_TOOLS,
    make_batched_tool,
    make_click_upload_tool,
    make_observe_tool,
)
from z_apply_core.config import load_settings
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
    configurable = config.get("configurable", {})
    prepared_runtime = configurable.get("prepared_runtime")
    if isinstance(prepared_runtime, RunRuntime):
        resources = configurable.get("run_resources")
        if isinstance(resources, RunResources):
            resources.runtime = prepared_runtime
        snapshot = await prepared_runtime.browser.tools.call("browser_snapshot")
        return {
            "snapshot": snapshot,
            "runtime": prepared_runtime,
            "browser_tools": _agent_browser_tools(prepared_runtime.browser),
        }
    display = VirtualDisplaySession(enabled=True)
    live_view = LiveView()
    browser: BrowserSession | None = None
    display.start()
    try:
        live_view.start(display.display, enabled=bool(state.get("live_view", True)))
        browser = await BrowserSession.start(display=display.display)
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
            context_inbox=(
                configurable.get("context_inbox")
                if isinstance(configurable.get("context_inbox"), ContextInbox)
                else None
            ),
        )
        resources = configurable.get("run_resources")
        if isinstance(resources, RunResources):
            resources.runtime = runtime
        return {
            "snapshot": snapshot,
            "runtime": runtime,
            "browser_tools": _agent_browser_tools(browser),
        }
    except Exception:
        if browser is not None:
            await browser.close()
        live_view.stop()
        display.stop()
        raise


def _agent_browser_tools(browser: BrowserSession) -> list[object]:
    settings = load_settings()
    safe_names: tuple[str, ...]
    if settings.browser_batch_tools:
        # Batch mode: one scripted mutation tool plus the cheap read tools. The
        # per-specialist filters keep browser_batched out of the auth/reviewer
        # agents and keep browser_take_screenshot available for VisionSpecialist.
        safe_names = ("browser_snapshot", "browser_find", "browser_take_screenshot")
        tools: list[object] = [
            *browser.tools.langchain_tools(safe_names),
            make_batched_tool(
                browser.run_action_batch,
                revision_provider=lambda: browser.last_observation_revision,
            ),
        ]
    else:
        safe_names = tuple(name for name in INITIAL_AGENT_BROWSER_TOOLS if name != "browser_tabs")
        tools = [*browser.tools.langchain_tools(safe_names)]
    return [
        *tools,
        make_observe_tool(browser.observe),
        make_click_upload_tool(
            browser.upload_files,
            default_paths=(str(settings.default_resume_path),),
            revision_provider=lambda: browser.last_observation_revision,
        ),
    ]
