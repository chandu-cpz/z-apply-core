from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.checkpoint.memory import InMemorySaver
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.goal_runner import ActiveGoalMiddleware, run_active_goal
from z_apply_core.agents.harness_profile import configure_z_apply_harness_profile
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.result import OrchestratorRun, RunStatus
from z_apply_core.agents.retry_policy import model_retry_middleware
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.safe_tool_batch import SafeToolBatchMiddleware
from z_apply_core.agents.specialists import build_specialists
from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware
from z_apply_core.application_artifacts import ApplicationArtifactPublisher
from z_apply_core.human.channel import HumanChannel
from z_apply_core.human.tools import make_human_tools
from z_apply_core.log_labels import node_info
from z_apply_core.memory.applicant_memory import CandidateMemory
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
        selection = await router.lease(tools=True, priority="balanced")
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
        human_tools = make_human_tools(
            human_channel,
            candidate_memory=candidate_memory,
            on_approval=record_approval,
            before_submit_approval=(
                artifact_publisher.publish_review_pdf
                if artifact_publisher is not None
                else None
            ),
            human_challenge_image_path=str(_captcha_path(run_id)),
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
            try:
                await artifact_publisher.publish_submission_screenshot()
            except Exception:
                logger.exception("Submission confirmation screenshot could not be published")
        terminal = ("completed", confirmation)
        return "Application submission recorded."

    @tool(return_direct=True)
    async def application_blocked(reason: str) -> str:
        """Finish when a concrete external dependency prevents remaining safe work."""
        nonlocal terminal
        terminal = ("incomplete", reason)
        return "Application blocker recorded."

    event_sink = SequencedEventSink(sink, run_id=run_id)
    router_middleware = NimRouterMiddleware(
        router,
        role="orchestrator",
        initial_selection=selection,
    )
    active_goal_middleware = ActiveGoalMiddleware(
        is_terminal=lambda: terminal is not None,
        on_no_progress=router_middleware.reject_active_response,
    )
    orchestrator_human_guard = HumanEscalationGuardMiddleware(
        allowed_reasons=frozenset({"human_challenge"})
    )
    answer_writer_human_guard = HumanEscalationGuardMiddleware(
        allowed_reasons=frozenset({"missing_candidate_fact", "ambiguous_field"})
    )
    agent = create_deep_agent(
        model=selection.llm,
        tools=[
            *browser_tools,
            *human_tools,
            application_submitted,
            application_blocked,
        ],
        system_prompt=load_prompt("orchestrator.md"),
        middleware=[
            SafeToolBatchMiddleware(),
            SubagentDispatchMiddleware(
                ["AnswerWriter", "AuthenticationSpecialist", "VisionSpecialist"]
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
            candidate_memory=candidate_memory,
            answer_writer_human_tools=[tool for tool in human_tools if tool.name == "ask_human"],
            answer_writer_middleware=[answer_writer_human_guard],
            authentication_tools=[
                *authentication_tools,
                *[tool for tool in human_tools if tool.name == "ask_human"],
            ],
        ),
        backend=FilesystemBackend(root_dir=CORE_ROOT, virtual_mode=True),
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
        await run_active_goal(
            agent,
            initial_message=prompt,
            config=run_config,
            sink=event_sink,
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

Simplify policy:
The Simplify addon is natively loaded in the persistent browser. Trigger its
visible page UI once on every newly rendered editable form step, before direct
resume/fact filling. A multi-step form may render a new step without changing
the URL. Never trigger it twice on the same unchanged set of controls. Observe
the actual form after every attempt and trust only visible field values, not an
extension success label. Unsupported sites and steps are normal. If its UI is
absent after one bounded inspection, reports unsupported, times out, or changes
nothing, stop looking for it on that step and continue direct filling
immediately.

Objective:
{task}

BEGIN UNTRUSTED CURRENT BROWSER EVIDENCE
{snapshot}
END UNTRUSTED CURRENT BROWSER EVIDENCE

Use browser tools directly. Finish only through application_submitted or
application_blocked. Submission requires explicit request_submit_approval.
"""


def _captcha_path(run_id: str) -> Path:
    return (
        Path.cwd() / ".z-apply" / "runs" / run_id / "browser-artifacts" / "captcha.png"
    ).resolve()
