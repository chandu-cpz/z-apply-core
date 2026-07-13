from __future__ import annotations

import contextlib

from langchain_core.runnables.config import RunnableConfig
from nim_router import NimRouter

from z_apply_core.agents.auth_orchestrator import run_auth_orchestrator
from z_apply_core.browser_tools import AUTH_AGENT_BROWSER_TOOLS
from z_apply_core.config import load_settings
from z_apply_core.gmail_tools import make_gmail_tools
from z_apply_core.human.tools import make_human_tools
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent, SequencedEventSink

SIMPLIFY_DASHBOARD_URL = "https://simplify.jobs/dashboard"


async def authenticate_default_account(
    state: RunState,
    config: RunnableConfig,
) -> dict[str, str]:
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime):
        return {"auth_status": "skipped", "auth_summary": "No live browser runtime is available."}

    settings = load_settings()
    original_url = str(state["job_url"])
    sink = SequencedEventSink(_sink_from_config(config), run_id=runtime.run_id)
    await _emit(sink, "started", "Opening Simplify auth check.")

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
            verification_tools=make_gmail_tools(
                credentials_path=settings.gmail_credentials_path,
                token_path=settings.gmail_token_path,
            ),
            config=config,
            sink=sink,
            router=router,
            default_credentials_available=settings.has_default_credentials,
        )

        restored_snapshot = await _restore_job_page(runtime, original_url)
        status = run.status
        await _emit(sink, status, run.summary)
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
        raise ValueError("configurable['nim_router'] is missing or not a NimRouter instance.")
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
