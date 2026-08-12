from __future__ import annotations

import logging

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool

from z_apply_core.agents.authentication import build_authentication_tools
from z_apply_core.agents.model_provider import provider_from_config
from z_apply_core.agents.orchestrator import run_orchestrator
from z_apply_core.application_artifacts import ApplicationArtifactPublisher
from z_apply_core.config import DEFAULT_RESUME_PATH, load_settings
from z_apply_core.gmail_tools import make_gmail_tools
from z_apply_core.human.channel import HumanChannel
from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState
from z_apply_core.stream_events import ledger_from_config, sink_from_config

_log = logging.getLogger(__name__)


async def orchestrator(state: RunState, config: RunnableConfig) -> dict[str, str]:
    sink = sink_from_config(config)
    provider = provider_from_config(config)
    runtime = _runtime(state)
    initial_snapshot = str(state.get("snapshot", ""))
    if runtime is not None:
        try:
            initial_snapshot = await runtime.browser.observe(full=True)
        except Exception as exc:  # noqa: BLE001 - the agent can recover by observing again
            _log.warning("Orchestrator handoff observation unavailable: %s", exc)
        runtime.browser.activate_submission_guard()
    authentication_tools = _authentication_tools(state, runtime)
    run = await run_orchestrator(
        job_url=str(state["job_url"]),
        task=str(state["task"]),
        snapshot=initial_snapshot,
        prompt_variant=str(state.get("prompt_variant", "") or ""),
        prompt_sha_override=str(state.get("prompt_sha", "") or ""),
        browser_tools=state.get("browser_tools", ()),
        authentication_tools=authentication_tools,
        config=config,
        call_ledger=ledger_from_config(config),
        human_channel=_human_channel(state),
        sink=sink,
        provider=provider,
        resume_path=str(DEFAULT_RESUME_PATH),
        candidate_memory=runtime_candidate_memory(state),
        run_id=_run_id(state),
        artifact_publisher=_artifact_publisher(state),
        on_submit_approval=(runtime.browser.set_submit_approval if runtime is not None else None),
        context_inbox=(runtime.context_inbox if runtime is not None else None),
        browser=(runtime.browser if runtime is not None else None),
    )
    snapshot = await _fresh_snapshot(state)
    return {
        "orchestrator_summary": run.summary,
        "model_id": run.model_id,
        "run_status": run.status,
        "snapshot": snapshot,
    }


async def _fresh_snapshot(state: RunState) -> str:
    fallback = str(state.get("snapshot", ""))
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime):
        return fallback
    try:
        snapshot = await runtime.browser.tools.call("browser_snapshot")
    except Exception as exc:  # noqa: BLE001 - preserve the completed run result
        _log.warning("Final browser snapshot unavailable: %s", exc)
        return fallback
    return snapshot or fallback


def _human_channel(state: RunState) -> HumanChannel | None:
    runtime = _runtime(state)
    if runtime is None or runtime.human_channel is None:
        return None
    return runtime.human_channel


def runtime_candidate_memory(state: RunState) -> CandidateMemory | None:
    runtime = _runtime(state)
    return runtime.candidate_memory if runtime is not None else None


def _run_id(state: RunState) -> str:
    runtime = _runtime(state)
    return runtime.run_id if runtime is not None else ""


def _artifact_publisher(state: RunState) -> ApplicationArtifactPublisher | None:
    runtime = _runtime(state)
    if runtime is None or runtime.human_channel is None:
        return None
    return ApplicationArtifactPublisher(
        browser=runtime.browser,
        channel=runtime.human_channel,
        on_created=runtime.artifact_callback,
    )


def _runtime(state: RunState) -> RunRuntime | None:
    runtime = state.get("runtime")
    return runtime if isinstance(runtime, RunRuntime) else None


def _authentication_tools(
    state: RunState,
    runtime: RunRuntime | None,
) -> list[BaseTool]:
    """The mid-run AuthenticationSpecialist's canonical tool set."""
    if runtime is None:
        return []
    settings = load_settings()
    return build_authentication_tools(
        browser_tools=state.get("browser_tools", ()),
        submit_auth_form=runtime.browser.submit_auth_form,
        open_verification_link=runtime.browser.open_verification_link,
        gmail_tools=make_gmail_tools(
            credentials_path=settings.gmail_credentials_path,
            token_path=settings.gmail_token_path,
        ),
        human_channel=runtime.human_channel,
    )
