from __future__ import annotations

import contextlib
import logging

from langchain_core.runnables.config import RunnableConfig

from z_apply_core.agents.auth_orchestrator import run_auth_orchestrator
from z_apply_core.agents.model_provider import provider_from_config
from z_apply_core.browser_tools import AUTH_AGENT_BROWSER_TOOLS
from z_apply_core.config import load_settings
from z_apply_core.gmail_tools import make_gmail_tools
from z_apply_core.human.tools import make_human_tools
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState
from z_apply_core.stream_events import (
    FrameworkEventSink,
    FrameworkTraceEvent,
    SequencedEventSink,
    ledger_from_config,
    sink_from_config,
)

SIMPLIFY_DASHBOARD_URL = "https://simplify.jobs/dashboard"

logger = logging.getLogger(__name__)


async def authenticate_default_account(
    state: RunState,
    config: RunnableConfig,
) -> dict[str, str]:
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime):
        return {"auth_status": "skipped", "auth_summary": "No live browser runtime is available."}

    settings = load_settings()
    original_url = str(state["job_url"])
    sink = SequencedEventSink(sink_from_config(config), run_id=runtime.run_id)
    await _emit(sink, "started", "Opening Simplify auth check.")

    try:
        snapshot = await runtime.browser.tools.call(
            "browser_navigate",
            {"url": SIMPLIFY_DASHBOARD_URL},
        )
        if not snapshot.startswith("### Error"):
            snapshot = await runtime.browser.tools.call("browser_snapshot")

        human_tools = (
            [
                tool
                for tool in make_human_tools(
                    runtime.human_channel,
                    capture_human_challenge=runtime.browser.capture_human_challenge,
                )
                if tool.name == "ask_human"
            ]
            if runtime.human_channel
            else []
        )
        provider = provider_from_config(config)
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
            provider=provider,
            ledger=ledger_from_config(config),
            default_credentials_available=settings.has_default_credentials,
            browser=runtime.browser,
        )

        status = run.status
        restored_snapshot = str(state.get("snapshot", ""))
        try:
            restored_snapshot = await _restore_job_page(runtime, original_url)
        except Exception as exc:
            # Restoring the job page is housekeeping, not the auth verdict. A
            # navigation abort (e.g. Firefox NS_ERROR_ABORT on a redirecting
            # ATS) must not turn a completed auth check into a failure; the
            # orchestrator re-observes the live page anyway.
            logger.warning(
                "Auth check %s but job page restore navigation failed: %s",
                status,
                exc,
            )
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
