from __future__ import annotations

import contextlib
import logging

from langchain_core.runnables.config import RunnableConfig

from z_apply_core.agents.authentication import (
    AUTHENTICATION_BROWSER_TOOLS,
    build_authentication_tools,
    run_authentication_agent,
)
from z_apply_core.agents.model_provider import provider_from_config
from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen
from z_apply_core.config import load_settings
from z_apply_core.gmail_tools import make_gmail_tools
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

# The dashboard can take tens of seconds to render in the supervised browser;
# the node settles it with bounded waits BEFORE the agent runs, so the agent
# starts from real page evidence instead of burning its turn budget on
# scaffolding snapshots (the historical 7x-wait / no-progress stall).
MAX_RENDER_SETTLE_WAITS = 3
RENDER_SETTLE_WAIT_SECONDS = 10

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
        snapshot = await _settle_page(runtime, snapshot)

        tools = build_authentication_tools(
            browser_tools=runtime.browser.tools.langchain_tools(AUTHENTICATION_BROWSER_TOOLS),
            submit_auth_form=runtime.browser.submit_auth_form,
            open_verification_link=runtime.browser.open_verification_link,
            gmail_tools=make_gmail_tools(
                credentials_path=settings.gmail_credentials_path,
                token_path=settings.gmail_token_path,
            ),
            human_channel=runtime.human_channel,
        )
        provider = provider_from_config(config)
        run = await run_authentication_agent(
            task=_task_prompt(
                snapshot=snapshot,
                default_credentials_available=settings.has_default_credentials,
            ),
            tools=tools,
            config=config,
            sink=sink,
            provider=provider,
            ledger=ledger_from_config(config),
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
        if _is_no_progress_stall(exc):
            # A stall in the pre-flight agent is not proof the session is
            # broken: the orchestrator re-observes the live page and can
            # delegate the same AuthenticationSpecialist mid-run, so surface
            # it as ambiguous rather than hard-blocking the application before
            # it starts.
            summary = f"Simplify auth check stalled: {exc}"
            with contextlib.suppress(Exception):
                await _restore_job_page(runtime, original_url)
            await _emit(sink, "not_verified", summary)
            return {
                "auth_status": "not_verified",
                "auth_summary": summary,
                "snapshot": str(state.get("snapshot", "")),
            }
        summary = f"Simplify auth check failed: {exc}"
        with contextlib.suppress(Exception):
            await _restore_job_page(runtime, original_url)
        await _emit(sink, "failed", summary)
        return {
            "auth_status": "failed",
            "auth_summary": summary,
            "snapshot": str(state.get("snapshot", "")),
        }


def _is_no_progress_stall(exc: Exception) -> bool:
    """True when the failure is a no-progress circuit trip (possibly wrapped).

    DeepAgents re-raises middleware exceptions through untyped SDK error
    paths, so match on both the class and the circuit's message patterns.
    """
    if isinstance(exc, NoProgressCircuitOpen):
        return True
    message = str(exc)
    return any(
        marker in message
        for marker in (
            "did not advance the browser state",
            "Repeated denied or non-progress tool calls",
            "repeated within the recent window without advancing",
            "repeatedly selected only bookkeeping or read tools",
        )
    )


async def _settle_page(runtime: RunRuntime, snapshot: str) -> str:
    """Wait (bounded) while the freshly opened page still shows loading scaffolding."""
    settled = snapshot
    for _ in range(MAX_RENDER_SETTLE_WAITS):
        if not _looks_like_scaffolding(settled):
            break
        await runtime.browser.tools.call(
            "browser_wait_for",
            {"time": RENDER_SETTLE_WAIT_SECONDS},
        )
        settled = await runtime.browser.tools.call("browser_snapshot")
    return settled


def _looks_like_scaffolding(snapshot: str) -> bool:
    if snapshot.startswith("### Error"):
        return False
    lowered = snapshot.casefold()
    if len(snapshot.strip()) < 120:
        return True
    return any(
        marker in lowered
        for marker in ("unnamed image", "empty alert", "loading scaffold", "still rendering")
    )


async def _restore_job_page(runtime: RunRuntime, original_url: str) -> str:
    restored_snapshot = await runtime.browser.tools.call(
        "browser_navigate",
        {"url": original_url},
    )
    if restored_snapshot.startswith("### Error"):
        return restored_snapshot
    return await runtime.browser.tools.call("browser_snapshot")


def _task_prompt(*, snapshot: str, default_credentials_available: bool) -> str:
    credential_status = (
        "DEFAULT_USERNAME and DEFAULT_PASSWORD are configured."
        if default_credentials_available
        else "No default credential secret keys are configured."
    )
    return f"""Verify or restore the default Simplify authentication.

Credential status: {credential_status}

BEGIN UNTRUSTED CURRENT BROWSER EVIDENCE
{snapshot}
END UNTRUSTED CURRENT BROWSER EVIDENCE

Begin with fresh browser evidence. If the page is still loading, wait at most
once and take one more snapshot. If the login form is visible: LOOK at the
form (get the exact current refs), fill both fields once with
DEFAULT_USERNAME and DEFAULT_PASSWORD, trust the fill receipt, and submit
immediately with browser_auth_submit. Never refill. A CAPTCHA after
submitting credentials is the expected path: call request_manual_auth and
stop until the human answers. Finish with exactly one AUTHENTICATED,
GATE_RESOLVED, or BLOCKED result marker.
"""


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
