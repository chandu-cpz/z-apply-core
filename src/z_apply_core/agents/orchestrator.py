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

from z_apply_core.agents.browser_mutation_serializer import SerializeBrowserMutationsMiddleware
from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.agents.context_inbox import ContextInbox, ContextInboxMiddleware
from z_apply_core.agents.goal_runner import (
    ActiveGoalExhausted,
    ActiveGoalMiddleware,
    run_persistent_goal,
)
from z_apply_core.agents.harness_profile import configure_z_apply_harness_profile
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.model_provider import ModelProvider, get_provider
from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen, NoProgressGuardMiddleware
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.readiness_verifier import require_submission_readiness
from z_apply_core.agents.result import OrchestratorRun, RunStatus
from z_apply_core.agents.retry_policy import model_retry_middleware
from z_apply_core.agents.router_middleware import (
    ORCHESTRATOR_EXCLUDED_MODEL_IDS,
    ModelRouter,
    build_router_middleware,
)
from z_apply_core.agents.specialists import build_specialists
from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware
from z_apply_core.application_artifacts import ApplicationArtifactPublisher
from z_apply_core.browser_session import BrowserSession
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

GOAL_STALL_LIMIT = 3


def decide_goal_stall(
    exc: Exception,
    stall_count: int,
    *,
    limit: int = GOAL_STALL_LIMIT,
    observation_signature: str | None = None,
    previous_signature: str | None = None,
) -> tuple[int, bool]:
    """Return ``(updated_stall_count, terminate)`` for one goal recovery event.

    Only no-progress circuit exceptions count toward the stall limit; any other
    recovery (provider timeouts, graph failures) resets the counter. Browser
    evidence advancing between no-progress events is real progress and resets
    the counter too, so a long healthy run that occasionally trips a per-turn
    circuit on separate stubborn controls is never falsely blocked. The frozen
    page signature of a genuine churn loop keeps the counter growing.
    """
    if not isinstance(exc, (NoProgressCircuitOpen, ActiveGoalExhausted)):
        return 0, False
    if (
        observation_signature is not None
        and previous_signature is not None
        and observation_signature != previous_signature
    ):
        stall_count = 0
    stall_count += 1
    return stall_count, stall_count >= limit


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

    await _seed_candidate_memory_from_resume(candidate_memory)
    # Embed the stored facts directly into the AnswerWriter prompt so they are
    # visible to the model even when a weak free-tier model skips the lookup
    # tool call. Sanitization (secrets, placeholders, masked values) happens at
    # the prompt boundary in build_answer_writer.
    candidate_facts: list[dict[str, object]] = (
        await candidate_memory.all_facts() if candidate_memory is not None else []
    )

    node_info(logger, "orchestrator", "initial model: %s", selection.info.id)
    approval: bool | None = None
    terminal: tuple[RunStatus, str] | None = None

    def record_approval(value: bool) -> None:
        nonlocal approval
        approval = value
        if on_submit_approval is not None:
            on_submit_approval(value)

    active_browser = browser or (
        artifact_publisher.browser if artifact_publisher is not None else None
    )
    event_sink = SequencedEventSink(sink, run_id=run_id)
    # Subagents get their own ask_human instance with the browser
    # challenge-capture path stripped: a subagent (e.g. AnswerWriter) must
    # never be able to drive the browser through ask_human internals, even
    # if a model misclassifies an upload control or CAPTCHA as a challenge.
    subagent_human_tools: list[BaseTool] = []
    submission_gate: list[BaseTool] = []
    if human_channel is not None:
        if active_browser is None:
            return OrchestratorRun(
                "A human channel is configured but no live browser is available "
                "for submission readiness review.",
                "",
                "failed",
            )

        async def prepare_submission_review(
            final_review: str,
            submission_target: str,
            url: str = "",
            company_name: str = "System",
            role_name: str = "Application",
        ) -> dict[str, object]:
            if artifact_publisher is not None:
                try:
                    await artifact_publisher.publish_review_artifact()
                except Exception as exc:  # noqa: BLE001 - artifacts must never block approval
                    logger.warning(
                        "Submission review artifact publish failed; continuing without it: %s",
                        exc,
                    )

            async def request_pending_approval(
                review_context: str,
            ) -> dict[str, object]:
                approved = await human_channel.confirm(
                    question="Submit this application?",
                    context=review_context,
                    url=url,
                    company=company_name,
                    role=role_name,
                )
                record_approval(approved)
                if approved:
                    return {"approved": True}
                correction = await human_channel.ask(
                    question="What should I correct before requesting submission approval again?",
                    context=(
                        "Submission was not approved. Give one precise correction or say "
                        "that the application should be stopped."
                    ),
                    url=url,
                    company=company_name,
                    role=role_name,
                )
                return {"approved": False, "correction": correction}

            verdict = await require_submission_readiness(
                browser=active_browser,
                provider=provider,
                final_review=final_review,
                config=config,
                sink=event_sink,
                run_id=run_id,
                request_pending_approval=request_pending_approval,
            )
            if verdict.ready:
                await active_browser.prepare_submission_review(
                    submission_target,
                )
            else:
                active_browser.set_submit_approval(False)
            return {
                "ready": verdict.ready,
                "submit_approval": verdict.submit_approval
                or ("approved" if verdict.ready else "not_ready"),
                "correction": verdict.correction,
                "evidence": verdict.evidence,
                "unresolved_required_fields": list(verdict.unresolved_required_fields),
                "visible_errors": list(verdict.visible_errors),
                "questionable_values": list(verdict.questionable_values),
            }

        @tool(return_direct=True)
        async def submit_application_review(
            final_review: str,
            submission_target: str,
            url: str = "",
            company_name: str = "System",
            role_name: str = "Application",
        ) -> dict[str, object]:
            """Pass the finished form through the independent readiness verifier.

            This handoff is the only path to pending submission approval: the
            verifier must record a ready verdict first, and it never asks the
            human while readiness findings are unresolved.
            """
            return await prepare_submission_review(
                final_review,
                submission_target,
                url,
                company_name,
                role_name,
            )

        submission_gate = [submit_application_review]

        human_tools = [
            tool
            for tool in make_human_tools(
                human_channel,
                candidate_memory=candidate_memory,
                capture_human_challenge=(
                    active_browser.capture_human_challenge
                    if active_browser is not None
                    else None
                ),
            )
            if tool.name == "ask_human"
        ]

        subagent_human_tools = [
            tool
            for tool in make_human_tools(
                human_channel,
                candidate_memory=candidate_memory,
                allow_human_challenge=False,
            )
            if tool.name == "ask_human"
        ]
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
                "Submission cannot finish until submit_application_review returns approved."
            )
        if artifact_publisher is not None:
            await artifact_publisher.publish_submission_screenshot()
        terminal = ("completed", confirmation)
        return "Application submission recorded."

    @tool(return_direct=True)
    async def application_blocked(reason: str, evidence: str = "") -> str:
        """Stop the run cleanly when a required value or action is unobtainable.

        Use only when fresh evidence proves the application cannot continue: a
        required candidate fact the human cannot supply, a page that rejects
        every legal action, or a human-declined blocking state. This is a clean
        terminal exit, never normal control flow.
        """
        nonlocal terminal
        summary = reason if not evidence else f"{reason}\n{evidence}"
        terminal = ("blocked", summary)
        return "Application blocked; the run stopped cleanly."

    run_context = RunContext(run_id=run_id)
    evidence_store = EvidenceStore(
        base_dir=CORE_ROOT / ".z-apply" / "runs" / run_id / "context"
    )
    if active_browser is not None:
        active_browser.bind_run_context(run_context)
        active_browser.bind_evidence_store(evidence_store)
    router_middleware = build_router_middleware(
        provider,
        role="orchestrator",
        selection=selection,
        sink=event_sink,
    )
    active_goal_middleware = ActiveGoalMiddleware(
        is_terminal=lambda: terminal is not None,
        on_no_progress=router_middleware.reject_active_response,
        sink=event_sink,
    )
    orchestrator_human_guard = HumanEscalationGuardMiddleware(
        allowed_reasons=frozenset({"human_challenge"})
    )
    orchestrator_browser_tools = [
        tool for tool in browser_tools if tool.name != "browser_take_screenshot"
    ]
    orchestrator_memory_tools = (
        make_candidate_memory_tools(candidate_memory)
        if candidate_memory is not None
        else ()
    )
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
    # One lock shared by the orchestrator and every specialist so browser
    # mutations never overlap even when a subagent and the orchestrator act in
    # the same response.
    mutation_lock = asyncio.Lock()
    agent = create_deep_agent(
        model=selection.llm,
        tools=[
            *orchestrator_browser_tools,
            *platform_memory_tools,
            *orchestrator_memory_tools,
            *human_tools,
            *submission_gate,
            application_blocked,
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
            router_middleware=router_middleware,
            orchestrator_human_guard=orchestrator_human_guard,
            active_goal_middleware=active_goal_middleware,
            terminal=terminal,
            mutation_lock=mutation_lock,
        ),
        subagents=await build_specialists(
            provider,
            browser_tools,
            fallback_model=selection.llm,
            candidate_resume=_candidate_resume_context(),
            answer_writer_candidate_facts=candidate_facts,
            answer_writer_human_tools=subagent_human_tools,
            answer_writer_memory_tools=(
                make_candidate_memory_tools(candidate_memory)
                if candidate_memory is not None
                else ()
            ),
            answer_writer_middleware=[
                HumanEscalationGuardMiddleware(
                    allowed_reasons=frozenset({"missing_candidate_fact", "ambiguous_field"})
                )
            ],
            authentication_tools=[
                *authentication_tools,
                *manual_auth_tools,
            ],
            sink=event_sink,
            mutation_lock=mutation_lock,
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
    stall_count = 0
    stall_signature: str | None = None

    def goal_recovery(exc: Exception, _attempt: int) -> bool:
        """End the run cleanly when the goal loop repeatedly makes no progress.

        Per-turn no-progress circuits only end the turn; the persistent goal then
        re-enters with fresh evidence and the same stuck page, which is how a
        churn loop outlives every middleware budget. Consecutive no-progress
        recoveries are converted into a clean ``application_blocked`` terminal.
        Browser evidence advancing between recoveries counts as real progress
        and resets the stall counter.
        """
        nonlocal stall_count, stall_signature, terminal
        observation = (
            active_browser.current_observation if active_browser is not None else None
        )
        signature = observation.signature if observation is not None else None
        stall_count, terminate = decide_goal_stall(
            exc,
            stall_count,
            observation_signature=signature,
            previous_signature=stall_signature,
        )
        if signature is not None:
            stall_signature = signature
        if not terminate:
            return False
        terminal = (
            "blocked",
            "stuck_loop: the application loop repeatedly made no progress "
            f"({stall_count} consecutive recoveries); stopping cleanly.",
        )
        logger.warning("orchestrator goal stalled; blocking the run cleanly")
        return True

    try:
        await run_persistent_goal(
            agent,
            initial_message=prompt,
            config=run_config,
            sink=event_sink,
            is_terminal=lambda: terminal is not None,
            on_recovery=goal_recovery,
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
    resume_section = f"Configured resume file for uploads: {resume_path}"
    if resume_text:
        resume_section += (
            "\nCandidate resume (authoritative candidate facts; use this text directly, never read files):\n"
            f"{resume_text}"
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
explicit submit_application_review. If ordinary work fails, recover through fresh
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


async def _seed_candidate_memory_from_resume(candidate_memory: CandidateMemory | None) -> None:
    """Preload resume profile facts into candidate memory for this run.

    This makes the AnswerWriter's mandatory exact-label lookup resolve
    identity facts (First name, Last name, Email, ...) without a human answer,
    so a model that insists on an exact memory match never loops on the same
    lookup or asks for a value the resume already contains. Explicit human
    answers always win over the resume copy; the memory method itself is
    best-effort and never blocks the run.
    """
    if candidate_memory is None:
        return
    for field_label, answer in _resume_profile_facts(_candidate_resume_context()).items():
        await candidate_memory.remember_resume_fact(
            field_label=field_label,
            answer=answer,
        )


def _resume_profile_facts(resume_text: str) -> dict[str, str]:
    """Parse ``- Key: Value`` lines under the resume's ``## Candidate Profile Facts`` section."""
    facts: dict[str, str] = {}
    in_section = False
    for line in resume_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped.casefold().startswith("## candidate profile facts")
            continue
        if not in_section:
            continue
        key, separator, value = (stripped[2:] if stripped.startswith("- ") else "").partition(
            ": "
        )
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            facts[key] = value
    return facts


def build_orchestrator_middleware(
    *,
    run_context: RunContext,
    evidence_store: EvidenceStore,
    event_sink: SequencedEventSink,
    active_browser: BrowserSession | None,
    platform_playbooks: PlatformPlaybooks,
    job_url: str,
    context_inbox: ContextInbox | None,
    router_middleware: ModelRouter,
    orchestrator_human_guard: HumanEscalationGuardMiddleware,
    active_goal_middleware: ActiveGoalMiddleware,
    terminal: tuple[RunStatus, str] | None = None,
    mutation_lock: asyncio.Lock | None = None,
) -> list[AgentMiddleware]:
    """Build the orchestrator middleware chain in execution order.

    The first element is the outermost wrapper. ``TokenMetricMiddleware``
    wraps every other middleware, and its emit adapter forwards typed events
    into the run-sequenced sink.
    """
    del terminal

    def usage_emit(event: object) -> None:
        _emit_usage_sync(event_sink, event)

    return [
        TokenMetricMiddleware(agent="orchestrator", run_context=run_context, emit=usage_emit),
        *([ContextInboxMiddleware(context_inbox)] if context_inbox is not None else []),
        CapabilityContextMiddleware(
            active_browser,
            platform_playbooks=platform_playbooks,
            job_url=job_url,
            run_context=run_context,
            evidence_store=evidence_store,
        ),
        NoProgressGuardMiddleware(
            browser=active_browser,
            on_no_progress=router_middleware.reject_active_response,
            max_stagnant_tool_calls=30,
            max_identical_denials=3,
            max_non_progress=8,
        ),
        SerializeBrowserMutationsMiddleware(sink=event_sink, lock=mutation_lock),
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
