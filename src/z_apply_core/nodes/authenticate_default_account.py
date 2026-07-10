from __future__ import annotations

import contextlib
import logging

from langchain_core.runnables.config import RunnableConfig
from nim_router import NimRouter

from z_apply_core.agents.auth_orchestrator import run_auth_orchestrator
from z_apply_core.browser_tools import AUTH_AGENT_BROWSER_TOOLS
from z_apply_core.config import load_settings
from z_apply_core.human.tools import make_human_tools
from z_apply_core.log_labels import node_info
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

logger = logging.getLogger(__name__)

SIMPLIFY_DASHBOARD_URL = "https://simplify.jobs/dashboard"


async def authenticate_default_account(
    state: RunState,
    config: RunnableConfig,
) -> dict[str, str]:
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime):
        return {"auth_status": "skipped", "auth_summary": "No live browser runtime is available."}

    settings = load_settings()
    if not settings.has_default_credentials:
        return {
            "auth_status": "skipped",
            "auth_summary": "Default credentials are not configured.",
        }

    original_url = str(state["job_url"])
    sink = _sink_from_config(config)
    await _emit(sink, "started", "Opening Simplify auth check.")
    node_info(logger, "authenticate_default_account", "opening Simplify auth check")

    try:
        snapshot = await runtime.browser.tools.call(
            "browser_navigate",
            {"url": SIMPLIFY_DASHBOARD_URL},
        )
        if not snapshot.startswith("### Error"):
            snapshot = await runtime.browser.tools.call("browser_snapshot")

        human_tools = make_human_tools(runtime.human_channel) if runtime.human_channel else []
        router = _router_from_config(config)
        run = await run_auth_orchestrator(
            snapshot=snapshot,
            browser_tools=runtime.browser.tools.langchain_tools(AUTH_AGENT_BROWSER_TOOLS),
            human_tools=human_tools,
            config=config,
            sink=sink,
            router=router,
        )

        restored_snapshot = await _restore_job_page(runtime, original_url)
        status = _status_from_summary(run.summary)
        await _emit(sink, status, run.summary)
        node_info(logger, "authenticate_default_account", "%s: %s", status, run.summary)
        return {
            "auth_status": status,
            "auth_summary": run.summary,
            "auth_model_id": run.model_id,
            "snapshot": restored_snapshot,
        }
    except Exception as exc:
        summary = f"Simplify auth check failed: {exc}"
        with contextlib.suppress(Exception):
            await _restore_job_page(runtime, original_url)
        await _emit(sink, "failed", summary)
        node_info(logger, "authenticate_default_account", "%s", summary)
        return {
            "auth_status": "failed",
            "auth_summary": summary,
            "snapshot": str(state.get("snapshot", "")),
        }


async def _restore_job_page(runtime: RunRuntime, original_url: str) -> str:
    restored_snapshot = await runtime.browser.tools.call(
        "browser_navigate",
        {"url": original_url},
    )
    if restored_snapshot.startswith("### Error"):
        return restored_snapshot
    return await runtime.browser.tools.call("browser_snapshot")


def _status_from_summary(summary: str) -> str:
    text = summary.lower()
    if "blocked" in text:
        return "blocked"
    if "not_verified" in text or "not verified" in text:
        return "not_verified"
    if "not authenticated" in text or "unauthenticated" in text:
        return "not_verified"
    if "authenticated" in text:
        return "authenticated"
    return "unknown"


def _sink_from_config(config: RunnableConfig) -> FrameworkEventSink | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    sink = configurable.get("sink")
    if hasattr(sink, "accept"):
        return sink
    return None


def _router_from_config(config: RunnableConfig) -> NimRouter:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise ValueError(
            "Run config is missing 'configurable'; cannot locate the shared NimRouter."
        )
    router = configurable.get("nim_router")
    if not isinstance(router, NimRouter):
        raise ValueError(
            "configurable['nim_router'] is missing or not a NimRouter instance."
        )
    return router


async def _emit(
    sink: FrameworkEventSink | None,
    status: str,
    summary: str,
) -> None:
    if sink is None:
        return
    await sink.accept(
        FrameworkTraceEvent(
            event="auth",
            name="authenticate_default_account",
            data={"status": status, "summary": summary},
            raw={},
        )
    )
