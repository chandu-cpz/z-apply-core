from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.checkpoint.memory import InMemorySaver
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.action_order import OrchestratorActionOrderMiddleware
from z_apply_core.agents.candidate_field import CandidateFieldMiddleware
from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.agents.context_inbox import ContextInbox, ContextInboxMiddleware
from z_apply_core.agents.goal_runner import ActiveGoalMiddleware, run_persistent_goal
from z_apply_core.agents.harness_profile import configure_z_apply_harness_profile
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
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
from z_apply_core.human.channel import HumanChannel
from z_apply_core.human.tools import make_human_tools, make_manual_auth_tool
from z_apply_core.log_labels import node_info
from z_apply_core.memory.applicant_memory import CandidateMemory
from z_apply_core.memory.platform_playbooks import (
    PlatformPlaybooks,
    make_platform_memory_tool,
)
from z_apply_core.stream_events import FrameworkEventSink, SequencedEventSink

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
    configure_z_apply_harness_profile()
    if not isinstance(router, NimRouter):
        return OrchestratorRun(
            "Model routing failed: shared NimRouter was not provided.",
            "",
            "failed",
        )

    try:
        selection = await router.lease(
            tools=True,
            reasoning=True,
            priority="balanced",
            excluded_model_ids=ORCHESTRATOR_EXCLUDED_MODEL_IDS,
        )
    except (NimRouterError, ImportError, ValueError) as exc:
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
                router=router,
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
    router_middleware = NimRouterMiddleware(
        router,
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
        middleware=[
            *([ContextInboxMiddleware(context_inbox)] if context_inbox is not None else []),
            CapabilityContextMiddleware(
                active_browser,
                platform_playbooks=platform_playbooks,
                job_url=job_url,
            ),
            SafeToolBatchMiddleware(),
            OrchestratorActionOrderMiddleware(active_browser),
            NoProgressGuardMiddleware(
                on_no_progress=router_middleware.reject_active_response,
            ),
            CandidateFieldMiddleware(
                active_browser,
                candidate_memory,
                next((tool for tool in human_tools if tool.name == "ask_human"), None),
            ),
            SubagentDispatchMiddleware(
                ["AnswerWriter", "AuthenticationSpecialist", "VisionSpecialist"],
                browser=active_browser,
            ),
            SummarizationMiddleware(
                model=selection.llm,
                backend=deepagent_backend,
                trigger=[("tokens", 24_000), ("messages", 36)],
                keep=("messages", 12),
                truncate_args_settings={
                    "trigger": ("messages", 16),
                    "keep": ("messages", 8),
                },
            ),
            model_retry_middleware(),
            router_middleware,
            ProseToolCallGuardMiddleware(),
            orchestrator_human_guard,
            active_goal_middleware,
        ],
        subagents=await build_specialists(
            router,
            browser_tools,
            fallback_model=selection.llm,
            candidate_resume=_candidate_resume_context(),
            answer_writer_human_tools=[],
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
    return f"""Complete this job application in the already-open browser.

Job URL: {job_url}
Configured resume: {resume_path}
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
    return path.read_text(encoding="utf-8")
