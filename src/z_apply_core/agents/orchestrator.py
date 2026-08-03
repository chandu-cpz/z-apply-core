from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import cast

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.checkpoint.memory import InMemorySaver
from nim_router import NimRouter

from z_apply_core.agents.action_order import OrchestratorActionOrderMiddleware
from z_apply_core.agents.candidate_field import CandidateFieldMiddleware
from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.agents.context_inbox import ContextInbox, ContextInboxMiddleware
from z_apply_core.agents.goal_runner import ActiveGoalMiddleware, run_persistent_goal
from z_apply_core.agents.harness_profile import configure_z_apply_harness_profile
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.model_provider import ModelProvider, get_provider
from z_apply_core.agents.no_progress_guard import NoProgressGuardMiddleware
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.readiness_verifier import require_submission_readiness
from z_apply_core.agents.result import OrchestratorRun, RunStatus
from z_apply_core.agents.retry_policy import model_retry_middleware
from z_apply_core.agents.router_middleware import (
    ORCHESTRATOR_EXCLUDED_MODEL_IDS,
    NimRouterMiddleware,
)
from z_apply_core.agents.safe_tool_batch import SafeToolBatchMiddleware
from z_apply_core.agents.specialists import build_specialists
from z_apply_core.agents.specialists.answer_writer import make_candidate_field_tool
from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware
from z_apply_core.application_artifacts import ApplicationArtifactPublisher
from z_apply_core.browser_session import BrowserSession
from z_apply_core.context.context_budget import ContextBudgetMiddleware
from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext
from z_apply_core.context.token_metric import TokenMetricMiddleware
from z_apply_core.human.channel import HumanChannel
from z_apply_core.human.tools import make_human_tools, make_manual_auth_tool
from z_apply_core.log_labels import node_info
from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.memory.platform_playbooks import (
    PlatformPlaybooks,
    make_platform_memory_tool,
)
from z_apply_core.memory.tools import make_candidate_memory_tools
from z_apply_core.stream_events import (
    FrameworkEventSink,
    FrameworkTraceEvent,
    SequencedEventSink,
)

logger = logging.getLogger(__name__)

CORE_ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS_VIRTUAL_ROOT = "/.z-apply/runs"
CANDIDATE_CONTEXT_VIRTUAL_PATH = "/chandrakanth_v_resume.md"


def deepagent_filesystem_permissions(run_id: str = "") -> list[FilesystemPermission]:
    artifact_root = (
        f"{ARTIFACTS_VIRTUAL_ROOT}/{run_id}/browser-artifacts" if run_id else ARTIFACTS_VIRTUAL_ROOT
    )
    return [
        FilesystemPermission(
            operations=["read"],
            paths=[artifact_root, f"{artifact_root}/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read"],
            paths=[CANDIDATE_CONTEXT_VIRTUAL_PATH],
            mode="allow",
        ),
        FilesystemPermission(operations=["read"], paths=["/**"], mode="deny"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]


DEEPAGENT_FILESYSTEM_PERMISSIONS = deepagent_filesystem_permissions()


async def run_orchestrator(
    *,
    job_url: str,
    task: str,
    snapshot: str,
    browser_tools: Sequence[BaseTool],
    config: RunnableConfig,
    human_tools: Sequence[BaseTool] = (),
    authentication_tools: Sequence[BaseTool] = (),
    sink: FrameworkEventSink | None = None,
    provider: ModelProvider | None = None,
    router: NimRouter | None = None,
    resume_path: str = "",
    candidate_memory: CandidateMemory | None = None,
    run_id: str = "",
    human_channel: HumanChannel | None = None,
    artifact_publisher: ApplicationArtifactPublisher | None = None,
    on_submit_approval: Callable[[bool], None] | None = None,
    context_inbox: ContextInbox | None = None,
    browser: BrowserSession | None = None,
) -> OrchestratorRun:
    """Run one persistent job-application agent against one shared browser."""
    from nim_router import NimRouter as NimRouterClass

    configure_z_apply_harness_profile()

    if provider is None:
        if router is None:
            return OrchestratorRun(
                "Model routing failed: neither provider nor router was provided.",
                "",
                "failed",
            )
        provider = get_provider(router)
    elif router is None and isinstance(provider, NimRouterClass):
        router = provider

    try:
        selection = await provider.lease(
            tools=True,
            reasoning=True,
            priority="balanced",
            excluded_model_ids=ORCHESTRATOR_EXCLUDED_MODEL_IDS,
        )
    except (ImportError, ValueError) as exc:
        return OrchestratorRun(f"Model selection failed: {exc}", "", "failed")

    node_info(logger, "orchestrator", "initial model: %s", selection.info.id)
    approval: bool | None = None
    terminal: tuple[RunStatus, str] | None = None

    def record_approval(value: bool) -> None:
        nonlocal approval
        approval = value
        if on_submit_approval is not None:
            on_submit_approval(value)

    if human_channel is not None:

        async def prepare_submission_review(
            final_review: str,
            submission_target: str,
        ) -> dict[str, object]:
            publisher = artifact_publisher
            if publisher is None:
                raise ToolException("Submission artifacts are unavailable for this run.")
            await publisher.publish_review_artifact()
            verdict = await require_submission_readiness(
                browser=publisher.browser,
                provider=provider,
                final_review=final_review,
                config=config,
                sink=event_sink,
                run_id=run_id,
            )
            if verdict.ready:
                await publisher.browser.prepare_submission_review(
                    submission_target,
                )
            else:
                publisher.browser.set_submit_approval(False)
            return {
                "ready": verdict.ready,
                "evidence": verdict.evidence,
                "unresolved_required_fields": list(verdict.unresolved_required_fields),
                "visible_errors": list(verdict.visible_errors),
                "questionable_values": list(verdict.questionable_values),
            }

        human_tools = make_human_tools(
            human_channel,
            candidate_memory=candidate_memory,
            on_approval=record_approval,
            before_submit_approval=(
                prepare_submission_review if artifact_publisher is not None else None
            ),
            capture_human_challenge=(
                artifact_publisher.browser.capture_human_challenge
                if artifact_publisher is not None
                else None
            ),
        )
    manual_auth_tools = (
        [
            make_manual_auth_tool(
                human_channel,
                human_challenge_image_path=str(_captcha_path(run_id)),
            )
        ]
        if human_channel is not None
        else []
    )

    @tool(return_direct=True)
    async def application_submitted(confirmation: str) -> str:
        """Finish after approval, final submit, and visible submission confirmation."""
        nonlocal terminal
        if approval is not True:
            raise ToolException(
                "Submission cannot finish until request_submit_approval returns approved."
            )
        if artifact_publisher is not None:
            await artifact_publisher.publish_submission_screenshot()
        terminal = ("completed", confirmation)
        return "Application submission recorded."

    event_sink = SequencedEventSink(sink, run_id=run_id)
    active_browser = browser or (
        artifact_publisher.browser if artifact_publisher is not None else None
    )
    run_context = RunContext(run_id=run_id)
    evidence_store = EvidenceStore(
        base_dir=CORE_ROOT / ".z-apply" / "runs" / run_id / "context"
    )
    if active_browser is not None:
        active_browser.bind_run_context(run_context)
        active_browser.bind_evidence_store(evidence_store)
    router_middleware = NimRouterMiddleware(
        provider,
        role="orchestrator",
        initial_selection=selection,
        sink=event_sink,
    )
    active_goal_middleware = ActiveGoalMiddleware(
        is_terminal=lambda: terminal is not None,
        on_no_progress=router_middleware.reject_active_response,
    )
    orchestrator_human_guard = HumanEscalationGuardMiddleware(
        allowed_reasons=frozenset({"human_challenge"})
    )
    orchestrator_browser_tools = [
        tool for tool in browser_tools if tool.name != "browser_take_screenshot"
    ]
    platform_playbooks = PlatformPlaybooks()
    platform_memory_tools = (
        [
            make_platform_memory_tool(
                platform_playbooks,
                job_url=job_url,
                browser=active_browser,
            )
        ]
        if active_browser is not None
        else []
    )
    deepagent_backend = FilesystemBackend(root_dir=CORE_ROOT, virtual_mode=True)
    agent = create_deep_agent(
        model=selection.llm,
        tools=[
            *orchestrator_browser_tools,
            *platform_memory_tools,
            make_candidate_field_tool(),
            *human_tools,
            application_submitted,
        ],
        system_prompt=load_prompt("orchestrator.md"),
        middleware=build_orchestrator_middleware(
            run_context=run_context,
            evidence_store=evidence_store,
            event_sink=event_sink,
            active_browser=active_browser,
            platform_playbooks=platform_playbooks,
            job_url=job_url,
            context_inbox=context_inbox,
            candidate_memory=candidate_memory,
            router_middleware=router_middleware,
            orchestrator_human_guard=orchestrator_human_guard,
            active_goal_middleware=active_goal_middleware,
            terminal=terminal,
        ),
        subagents=await build_specialists(
            provider,
            browser_tools,
            fallback_model=selection.llm,
            candidate_resume=_candidate_resume_context(),
            answer_writer_human_tools=[
                tool for tool in human_tools if tool.name == "ask_human"
            ],
            answer_writer_memory_tools=(
                make_candidate_memory_tools(candidate_memory)
                if candidate_memory is not None
                else ()
            ),
            answer_writer_middleware=[
                HumanEscalationGuardMiddleware(
                    allowed_reasons=frozenset({"ambiguous_field"})
                )
            ],
            authentication_tools=[
                *authentication_tools,
                *manual_auth_tools,
            ],
            sink=event_sink,
        ),
        backend=deepagent_backend,
        permissions=deepagent_filesystem_permissions(run_id),
        checkpointer=InMemorySaver(),
    )

    run_config = cast(RunnableConfig, config.copy() if config else {})
    configurable = dict(run_config.get("configurable", {}))
    configurable["thread_id"] = f"z-apply:{run_id}"
    run_config["configurable"] = configurable
    prompt = _task_prompt(
        job_url=job_url,
        task=task,
        snapshot=snapshot,
        resume_path=resume_path,
        run_id=run_id,
    )
    try:
        await run_persistent_goal(
            agent,
            initial_message=prompt,
            config=run_config,
            sink=event_sink,
            is_terminal=lambda: terminal is not None,
        )
    except Exception as exc:  # noqa: BLE001 - return a clear infrastructure status
        logger.exception("Persistent job-application agent failed")
        return OrchestratorRun(
            f"Agent execution failed after model recovery was exhausted: {exc}",
            router_middleware.last_model_id,
            "failed",
        )

    if terminal is None:
        return OrchestratorRun(
            "Agent stopped without recording submission or a concrete blocker.",
            router_middleware.last_model_id,
            "failed",
        )
    status, summary = terminal
    return OrchestratorRun(summary, router_middleware.last_model_id, status)


def _task_prompt(
    *,
    job_url: str,
    task: str,
    snapshot: str,
    resume_path: str,
    run_id: str,
) -> str:
    captcha_path = _captcha_path(run_id)
    resume_text = _candidate_resume_context()
    resume_section = (
        f"Candidate resume (authoritative candidate facts; use this text directly, never read files):\n{resume_text}"
        if resume_text
        else f"Configured resume: {resume_path}"
    )
    return f"""Complete this job application in the already-open browser.

Job URL: {job_url}
{resume_section}
CAPTCHA artifact path: {captcha_path}

Objective:
{task}

BEGIN UNTRUSTED CURRENT BROWSER EVIDENCE
{snapshot}
END UNTRUSTED CURRENT BROWSER EVIDENCE

Use browser tools directly. Finish only through application_submitted after
explicit request_submit_approval. If ordinary work fails, recover through fresh
evidence and another legal action; do not invent a terminal blocker.
"""


def _captcha_path(run_id: str) -> Path:
    return (
        CORE_ROOT / ".z-apply" / "runs" / run_id / "browser-artifacts" / "captcha.png"
    ).resolve()


def _candidate_resume_context() -> str:
    path = CORE_ROOT / CANDIDATE_CONTEXT_VIRTUAL_PATH.lstrip("/")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Candidate resume not readable at %s", path)
        return ""


def build_orchestrator_middleware(
    *,
    run_context: RunContext,
    evidence_store: EvidenceStore,
    event_sink: SequencedEventSink,
    active_browser: BrowserSession | None,
    platform_playbooks: PlatformPlaybooks,
    job_url: str,
    context_inbox: ContextInbox | None,
    candidate_memory: CandidateMemory | None,
    router_middleware: NimRouterMiddleware,
    orchestrator_human_guard: HumanEscalationGuardMiddleware,
    active_goal_middleware: ActiveGoalMiddleware,
    terminal: tuple[RunStatus, str] | None = None,
) -> list[AgentMiddleware]:
    """Build the orchestrator middleware chain in execution order.

    The first element is the outermost wrapper. ``ContextBudgetMiddleware`` and
    ``TokenMetricMiddleware`` wrap every other middleware, and their emit
    adapters forward typed events into the run-sequenced sink.
    """
    del terminal

    def usage_emit(event: object) -> None:
        _emit_usage_sync(event_sink, event)

    return [
        ContextBudgetMiddleware(
            evidence_store=evidence_store,
            run_context=run_context,
        ),
        TokenMetricMiddleware(agent="orchestrator", run_context=run_context, emit=usage_emit),
        *([ContextInboxMiddleware(context_inbox)] if context_inbox is not None else []),
        CapabilityContextMiddleware(
            active_browser,
            platform_playbooks=platform_playbooks,
            job_url=job_url,
            run_context=run_context,
            evidence_store=evidence_store,
        ),
        SafeToolBatchMiddleware(),
        OrchestratorActionOrderMiddleware(active_browser),
        NoProgressGuardMiddleware(
            on_no_progress=router_middleware.reject_active_response,
        ),
        CandidateFieldMiddleware(
            active_browser,
            candidate_memory,
            run_context=run_context,
        ),
        SubagentDispatchMiddleware(
            ["AnswerWriter", "AuthenticationSpecialist", "VisionSpecialist"],
            browser=active_browser,
        ),
        model_retry_middleware(),
        router_middleware,
        ProseToolCallGuardMiddleware(),
        orchestrator_human_guard,
        active_goal_middleware,
    ]


def _emit_usage_sync(sink: SequencedEventSink, event: object) -> None:
    """Emit a token usage event into a run-sequenced sink, fire-and-forget.

    ``TokenMetricMiddleware`` requires a synchronous ``emit``, but
    ``SequencedEventSink.accept`` is async. The event is wrapped into a
    ``FrameworkTraceEvent`` and the resulting coroutine is scheduled on the
    running event loop instead of being awaited.
    """
    result = sink.accept(_as_trace_event("token_usage", event))
    if inspect.isawaitable(result):
        asyncio.get_running_loop().create_task(result)


def _as_trace_event(kind: str, event: object) -> FrameworkTraceEvent:
    """Wrap a typed runtime event into the trace event the sequenced sink reads."""
    if isinstance(event, FrameworkTraceEvent):
        return event
    return FrameworkTraceEvent(
        event=kind,
        name=kind,
        data=_event_data(event),
        raw={},
    )


def _event_data(event: object) -> dict[str, object]:
    if is_dataclass(event) and not isinstance(event, type):
        return {field.name: getattr(event, field.name) for field in fields(event)}
    return {"value": event}
