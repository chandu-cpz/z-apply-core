from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, cast

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.checkpoint.memory import InMemorySaver

from z_apply_core.agents.authentication import captcha_artifact_path
from z_apply_core.agents.context_inbox import ContextInbox
from z_apply_core.agents.delegation_guard import (
    DelegationFailureLadder,
    DelegationResultMiddleware,
)
from z_apply_core.agents.goal_runner import (
    ActiveGoalExhausted,
    ActiveGoalMiddleware,
    run_persistent_goal,
)
from z_apply_core.agents.harness_profile import (
    CANDIDATE_CONTEXT_VIRTUAL_PATH,
    configure_z_apply_harness_profile,
    deepagent_filesystem_permissions,
)
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.middleware_factory import build_agent_middleware
from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen
from z_apply_core.agents.prompts import ORCHESTRATOR_PROMPT, load_prompt
from z_apply_core.agents.providers import ModelGateway, get_model_gateway
from z_apply_core.agents.result import OrchestratorRun, RunStatus
from z_apply_core.agents.router_middleware import (
    ModelRouter,
    build_router_middleware,
)
from z_apply_core.agents.specialists import build_specialists
from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware
from z_apply_core.agents.summarization_observability import (
    install_summarization_observability,
    reset_summarization_observer,
    set_summarization_observer,
)
from z_apply_core.application_artifacts import ApplicationArtifactPublisher
from z_apply_core.browser_session import BrowserSession
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext
from z_apply_core.human.channel import HumanChannel
from z_apply_core.human.tools import make_human_tools
from z_apply_core.log_labels import node_info
from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.memory.platform_playbooks import (
    PlatformPlaybooks,
    make_platform_memory_tool,
)
from z_apply_core.memory.tools import make_candidate_memory_tools
from z_apply_core.paths import CORE_ROOT, run_context_dir
from z_apply_core.stream_events import (
    FrameworkEventSink,
    FrameworkTraceEvent,
    SequencedEventSink,
)
from z_apply_core.text_utils import clean_job_metadata

logger = logging.getLogger(__name__)

GOAL_STALL_LIMIT = 2


def _make_summarization_observer(
    sink: FrameworkEventSink | None,
) -> Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]:
    """Route summarizer model-call telemetry into the run's sequenced sink."""

    async def observe(event_name: str, data: dict[str, Any]) -> None:
        if sink is None:
            return
        await sink.accept(
            FrameworkTraceEvent(event=event_name, name="summarization", data=data, raw={})
        )

    return observe


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


def build_escalation_stack(
    *,
    ladder_threshold: int = 2,
) -> tuple[
    HumanEscalationGuardMiddleware,
    HumanEscalationGuardMiddleware,
    DelegationResultMiddleware,
]:
    """Build the per-run escalation gates around ONE shared failure ladder.

    Returns ``(orchestrator_guard, answer_writer_guard, delegation_result)``.
    The ladder MUST reach every gate: the delegation-result middleware records
    empty specialist outputs, and once failures reach ``ladder_threshold`` the
    orchestrator's guard — the agent that actually calls ``ask_human`` — opens
    ``missing_candidate_fact``. Wiring the ladder into only the delegate-side
    guard recreates the FAIL-003 deadlock (deny survives proven delegation
    failure), so this factory is the single construction path and the wiring
    test asserts through it.
    """
    ladder = DelegationFailureLadder(threshold=ladder_threshold)
    return (
        HumanEscalationGuardMiddleware(
            allowed_reasons=frozenset({"human_challenge"}),
            delegation_ladder=ladder,
        ),
        HumanEscalationGuardMiddleware(
            allowed_reasons=frozenset({"missing_candidate_fact", "ambiguous_field"}),
            delegation_ladder=ladder,
        ),
        DelegationResultMiddleware(ladder),
    )


def make_report_job_metadata(
    metadata_reporter: Callable[[str, str, str | None], None] | None,
) -> BaseTool:
    """Build the orchestrator tool that records company/role on the live run view.

    The reporter is bound by the service layer to ``run.view``; when absent
    (CLI runs) the tool still validates and reports that it cannot persist.
    """

    @tool
    async def report_job_metadata(company: str, role: str, location: str | None = None) -> str:
        """Record the company and role for this application on the run view.

        Call it once, before any form filling or autofill (including
        Simplify), using values read from the job page. Company and role
        title are required; include location when the page shows one.
        """
        cleaned_company = clean_job_metadata(company)
        cleaned_role = clean_job_metadata(role)
        cleaned_location = clean_job_metadata(location) if location else ""
        if not cleaned_company or not cleaned_role:
            return (
                "report_job_metadata rejected: company and role are required "
                "non-empty strings read from the page. Re-call with real values."
            )
        if metadata_reporter is None:
            return "Metadata reporting is unavailable for this run; continue the application."
        metadata_reporter(cleaned_company, cleaned_role, cleaned_location or None)
        suffix = " (location accepted, not persisted yet)" if cleaned_location else ""
        return f"Recorded job metadata: company={cleaned_company}, role={cleaned_role}{suffix}."

    return report_job_metadata


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
    provider: ModelGateway | None = None,
    resume_path: str = "",
    candidate_memory: CandidateMemory | None = None,
    run_id: str = "",
    human_channel: HumanChannel | None = None,
    artifact_publisher: ApplicationArtifactPublisher | None = None,
    on_submit_approval: Callable[[bool], None] | None = None,
    context_inbox: ContextInbox | None = None,
    browser: BrowserSession | None = None,
    call_ledger: RunCallLedger | None = None,
    metadata_reporter: Callable[[str, str, str | None], None] | None = None,
    capability_context_mode: str | None = None,
) -> OrchestratorRun:
    """Run one persistent job-application agent against one shared browser."""
    configure_z_apply_harness_profile()

    if provider is None:
        try:
            provider = get_model_gateway()
        except ValueError as exc:
            return OrchestratorRun(f"Model routing failed: {exc}", "", "failed")

    try:
        llm = provider.get_model()
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

    node_info(logger, "orchestrator", "initial model: %s", provider.model_id)
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
    # FAIL-006: deepagents' summarizer makes full-history LLM calls OUTSIDE our
    # middleware chain; without this seam those minutes of wall clock emit zero
    # events. Install once, then route its model-call telemetry into the run's
    # own sequenced sink for the duration of the graph execution.
    install_summarization_observability()
    summarization_observer_token = set_summarization_observer(
        _make_summarization_observer(event_sink)
    )
    # Subagents get their own ask_human instance with the browser
    # challenge-capture path stripped: a subagent (e.g. AnswerWriter) must
    # never be able to drive the browser through ask_human internals, even
    # if a model misclassifies an upload control or CAPTCHA as a challenge.
    subagent_human_tools: list[BaseTool] = []
    submission_reviewer_tools: list[BaseTool] = []
    if human_channel is not None:
        if active_browser is None:
            return OrchestratorRun(
                "A human channel is configured but no live browser is available "
                "for submission review.",
                "",
                "failed",
            )

        async def request_pending_approval(
            review_context: str,
        ) -> dict[str, object]:
            if approval is True:
                # Approval sticks only for a submission that verifiably
                # succeeded. If a submit click already consumed the approval
                # (guard consumed) and we are being asked again, the previous
                # submission did NOT go through — revoke and require FRESH
                # human consent instead of silently re-approving.
                if active_browser is not None and active_browser.submission_consumed():
                    record_approval(False)
                else:
                    return {"approved": True}
            approved = await human_channel.confirm(
                question="Submit this application?",
                context=review_context,
                url=job_url,
                company="System",
                role="Application",
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
                url=job_url,
                company="System",
                role="Application",
            )
            return {"approved": False, "correction": correction}

        @tool
        async def publish_review_artifact() -> str:
            """Deliver the full-page application review image to the human channel.

            Call it immediately before requesting submission approval so the
            human reviews the exact current form.
            """
            if artifact_publisher is None:
                return (
                    "No review image publisher is available; the human reviews the "
                    "form in the browser directly."
                )
            try:
                await artifact_publisher.publish_review_artifact()
                return "Full-page review image published to the human channel."
            except Exception as exc:  # noqa: BLE001 - artifacts must never block approval
                logger.warning(
                    "Submission review artifact publish failed; continuing without it: %s",
                    exc,
                )
                return "Review image publish failed; continuing without it."

        @tool
        async def request_submission_approval(context: str) -> str:
            """Ask the human to approve final submission for this application.

            The full-page review image is already delivered to the human.
            Returns ``APPROVED``, or ``REJECTED: <correction>`` with the
            human's precise correction when they declined. The human is never
            asked twice for one application.
            """
            outcome = await request_pending_approval(context)
            if bool(outcome.get("approved", False)):
                return "APPROVED"
            correction = str(outcome.get("correction", "")).strip()
            return f"REJECTED: {correction}" if correction else "REJECTED"

        @tool
        async def submit_approved_application() -> str:
            """Click the final submit control under the armed guard.

            The runtime resolves the control from live DOM, verifies it is a
            real form submit, and performs the one-use guarded click. Returns
            fresh post-click evidence; read it to judge the outcome. Truth
            events emit at the executor layer (browser_session), which stays
            visible even through recovery turns.
            """
            return await active_browser.submit_approved_application()

        submission_reviewer_tools = [
            publish_review_artifact,
            request_submission_approval,
            submit_approved_application,
            # PROP-005 S3 backstop: the reviewer verifies every material value
            # against candidate reality through read-only lookups, so fabricated
            # values (FAIL-007) are rejected before the human is involved.
            *(make_candidate_memory_tools(candidate_memory) if candidate_memory else ()),
        ]

        human_tools = [
            tool
            for tool in make_human_tools(
                human_channel,
                candidate_memory=candidate_memory,
                capture_human_challenge=(
                    active_browser.capture_human_challenge if active_browser is not None else None
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

    @tool(return_direct=True)
    async def application_submitted(confirmation: str) -> str:
        """Finish after approval, final submit, and visible submission confirmation."""
        nonlocal terminal
        if approval is not True:
            raise ToolException(
                "Submission cannot finish until the Submission Reviewer reports "
                "SUBMITTED after the human approved."
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
    evidence_store = EvidenceStore(base_dir=run_context_dir(run_id))
    if active_browser is not None:
        active_browser.bind_run_context(run_context)
        active_browser.bind_evidence_store(evidence_store)
        active_browser.bind_event_sink(event_sink)
    router_middleware = build_router_middleware(
        provider,
        role="orchestrator",
        sink=event_sink,
        ledger=call_ledger,
    )
    active_goal_middleware = ActiveGoalMiddleware(
        is_terminal=lambda: terminal is not None,
        on_no_progress=router_middleware.reject_active_response,
        sink=event_sink,
    )
    # One ladder per run, shared by EVERY escalation gate: the orchestrator's
    # guard (the agent that calls ask_human), the AnswerWriter's guard, and
    # the delegation-result middleware (records failures). Attaching it only
    # to the delegate side would leave the orchestrator's own deny branch
    # firing forever — the exact FAIL-003 deadlock this ladder exists to break.
    orchestrator_human_guard, answer_writer_human_guard, delegation_result_middleware = (
        build_escalation_stack()
    )
    orchestrator_browser_tools = [
        tool for tool in browser_tools if tool.name != "browser_take_screenshot"
    ]
    orchestrator_memory_tools = (
        make_candidate_memory_tools(candidate_memory) if candidate_memory is not None else ()
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
    deepagent_backend = FilesystemBackend(
        root_dir=CORE_ROOT, virtual_mode=True
    )  # repo root for virtual FS
    # One lock shared by the orchestrator and every specialist so browser
    # mutations never overlap even when a subagent and the orchestrator act in
    # the same response.
    mutation_lock = asyncio.Lock()
    agent = create_deep_agent(
        model=llm,
        tools=[
            *orchestrator_browser_tools,
            *platform_memory_tools,
            *orchestrator_memory_tools,
            *human_tools,
            application_blocked,
            application_submitted,
            make_report_job_metadata(metadata_reporter),
        ],
        system_prompt=load_prompt(ORCHESTRATOR_PROMPT),
        middleware=build_orchestrator_middleware(
            provider=provider,
            run_context=run_context,
            evidence_store=evidence_store,
            event_sink=event_sink,
            active_browser=active_browser,
            platform_playbooks=platform_playbooks,
            job_url=job_url,
            context_inbox=context_inbox,
            router_middleware=router_middleware,
            orchestrator_human_guard=orchestrator_human_guard,
            delegation_result_middleware=delegation_result_middleware,
            active_goal_middleware=active_goal_middleware,
            mutation_lock=mutation_lock,
            capability_context_mode=capability_context_mode,
        ),
        subagents=await build_specialists(
            provider,
            browser_tools,
            fallback_model=llm,
            candidate_resume=_candidate_resume_context(),
            answer_writer_candidate_facts=candidate_facts,
            answer_writer_human_tools=subagent_human_tools,
            answer_writer_memory_tools=(
                make_candidate_memory_tools(candidate_memory)
                if candidate_memory is not None
                else ()
            ),
            answer_writer_middleware=[answer_writer_human_guard],
            authentication_tools=[
                *authentication_tools,
            ],
            submission_reviewer_tools=submission_reviewer_tools,
            sink=event_sink,
            mutation_lock=mutation_lock,
            ledger=call_ledger,
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
        observation = active_browser.current_observation if active_browser is not None else None
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
    finally:
        reset_summarization_observer(summarization_observer_token)

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
    captcha_path = captcha_artifact_path(run_id)
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
the Submission Reviewer reports SUBMITTED. If ordinary work fails, recover through fresh
evidence and another legal action; do not invent a terminal blocker.
"""


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
        key, separator, value = (stripped[2:] if stripped.startswith("- ") else "").partition(": ")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            facts[key] = value
    return facts


def build_orchestrator_middleware(
    *,
    provider: ModelGateway | None = None,
    run_context: RunContext,
    evidence_store: EvidenceStore,
    event_sink: SequencedEventSink,
    active_browser: BrowserSession | None,
    platform_playbooks: PlatformPlaybooks,
    job_url: str,
    context_inbox: ContextInbox | None,
    router_middleware: ModelRouter,
    orchestrator_human_guard: HumanEscalationGuardMiddleware,
    delegation_result_middleware: DelegationResultMiddleware | None = None,
    active_goal_middleware: ActiveGoalMiddleware,
    mutation_lock: asyncio.Lock | None = None,
    capability_context_mode: str | None = None,
) -> list[AgentMiddleware]:
    """Build the orchestrator middleware chain via single factory.

    Delegates to :func:`build_agent_middleware` so all agents share one
    ordering invariant. See factory docstring for skeleton.
    """
    base = build_agent_middleware(
        role="orchestrator",
        provider=provider,
        run_context=run_context,
        evidence_store=evidence_store,
        event_sink=event_sink,
        active_browser=active_browser,
        platform_playbooks=platform_playbooks,
        job_url=job_url,
        context_inbox=context_inbox,
        router_middleware=router_middleware,
        human_guard=orchestrator_human_guard,
        no_progress_kwargs={
            "max_stagnant_tool_calls": 12,
            "max_identical_denials": 3,
            "max_non_progress": 6,
            "window_size": 6,
            "repetition_threshold": 3,
        },
        extra_middleware=[
            SubagentDispatchMiddleware(
                [
                    "AnswerWriter",
                    "AuthenticationSpecialist",
                    "SubmissionReviewer",
                    "VisionSpecialist",
                ],
                browser=active_browser,
            ),
            *([delegation_result_middleware] if delegation_result_middleware is not None else []),
        ],
        mutation_lock=mutation_lock,
        capability_context_mode=capability_context_mode,
    )
    # ActiveGoal is innermost, after human guard
    return [*base, active_goal_middleware]
