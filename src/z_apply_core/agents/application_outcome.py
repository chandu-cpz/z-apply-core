from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.application_state import ApplicationState
from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.retry_policy import should_retry_model_error
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.terminal_guard import (
    TerminalDecisionGuardMiddleware,
    TerminalDecisionRecorded,
)
from z_apply_core.stream_events import FrameworkEventSink, V3RunResult

APPLICATION_OUTCOME_RUBRIC = """Evaluate the complete observable run against every criterion:

- The application form is open and its current state has been observed.
- If a primary resume or CV control is available, the configured resume was uploaded and the
  resulting filename, parsed profile data, or another visible upload-success state was verified.
- Visible application fields were mapped and every safe answer supported by candidate evidence
  was filled.
- Every required answer unavailable from candidate evidence was requested from the human.
- The filled form was independently reviewed for missing required fields and contradictory values.
- Submission approval was requested only after the application became review-ready.
- The final submit control was not activated by this runtime.

Do not accept plans, intentions, todo labels, or an agent's claim as proof. Require browser/tool
evidence for browser state and human-tool evidence for approval state. A CAPTCHA beside final
submit does not prevent preparation work and is not a blocker until submission is authorized.
"""

MAX_OUTCOME_ITERATIONS = 8
MAX_OUTCOME_VERDICT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class OutcomeDecision:
    status: Literal["satisfied", "needs_revision", "blocked", "failed"]
    explanation: str


async def evaluate_application_outcome(
    *,
    task: str,
    output: dict[str, Any],
    tool_journal: Sequence[dict[str, Any]],
    snapshot: str,
    router: NimRouter,
    sink: FrameworkEventSink | None,
    application_state: ApplicationState | None = None,
) -> OutcomeDecision:
    del output, tool_journal
    decision: OutcomeDecision | None = None

    @tool
    async def outcome_satisfied(summary: str) -> str:
        """Record that every application outcome criterion is proven by current evidence."""
        nonlocal decision
        if decision is not None:
            return "An outcome transition has already been recorded for this evaluation."
        decision = OutcomeDecision("satisfied", summary)
        return "Outcome recorded as satisfied."

    @tool
    async def outcome_needs_revision(feedback: str) -> str:
        """Return evidence-based audit feedback about missing or contradictory criteria."""
        nonlocal decision
        if decision is not None:
            return "An outcome transition has already been recorded for this evaluation."
        decision = OutcomeDecision("needs_revision", feedback)
        return "Revision request recorded."

    @tool
    async def outcome_blocked(reason: str) -> str:
        """Record a concrete unresolved dependency that prevents further safe progress."""
        nonlocal decision
        if decision is not None:
            return "An outcome transition has already been recorded for this evaluation."
        decision = OutcomeDecision("blocked", reason)
        return "Blocking dependency recorded."

    try:
        selection = await router.lease(tools=True, priority="balanced", reasoning=False)
    except (NimRouterError, ImportError, ValueError) as exc:
        return OutcomeDecision("failed", f"Outcome evaluator model selection failed: {exc}")

    evaluator_router = NimRouterMiddleware(
        router,
        role="GoalEvaluator",
        initial_selection=selection,
    )
    evaluator = create_deep_agent(
        model=selection.llm,
        tools=[outcome_satisfied, outcome_needs_revision, outcome_blocked],
        system_prompt=load_prompt("goal_evaluator.md"),
        middleware=[
            ModelRetryMiddleware(
                max_retries=1, retry_on=should_retry_model_error, on_failure="error"
            ),
            evaluator_router,
            ProseToolCallGuardMiddleware(),
            TerminalDecisionGuardMiddleware(lambda: decision is not None),
        ],
    )
    prompt = _evaluation_prompt(
        task=task,
        snapshot=snapshot,
        application_state=application_state,
    )
    errors: list[str] = []
    attempt_input: dict[str, Any] = {"messages": [{"role": "user", "content": prompt}]}
    for _attempt in range(1, MAX_OUTCOME_VERDICT_ATTEMPTS + 1):
        try:
            stream = evaluator.astream_events(
                cast(Any, attempt_input),
                version="v3",
            )
            await consume_deepagent_stream(
                stream,
                sink=sink,
                root_source="GoalEvaluator",
            )
        except TerminalDecisionRecorded:
            if decision is not None:
                return decision
            raise
        except Exception as exc:  # noqa: BLE001 - report an explicit runtime failure
            errors.append(str(exc))
            attempt_input = _clean_outcome_retry_input(
                prompt,
                "The previous evaluator model call failed technically.",
            )
            continue
        if decision is not None:
            return decision
        attempt_input = _clean_outcome_retry_input(
            prompt,
            "The previous evaluator returned without a typed outcome transition.",
        )

    detail = "; ".join(errors) if errors else "no outcome transition tool was called"
    return OutcomeDecision(
        "failed",
        (
            "Outcome evaluation failed to record a typed decision after "
            f"{MAX_OUTCOME_VERDICT_ATTEMPTS} attempts ({detail}). "
            "The runtime stopped safely without making another browser change."
        ),
    )


def _clean_outcome_retry_input(prompt: str, failure: str) -> dict[str, Any]:
    return {
        "messages": [
            HumanMessage(
                content=(
                    f"{prompt}\n\n"
                    f"{failure} Discard that model's prose and independently audit the "
                    "application evidence above. Call exactly one of outcome_satisfied, "
                    "outcome_needs_revision, or outcome_blocked now. Tool arguments must "
                    "describe the application state and next application action, never this "
                    "evaluator protocol."
                )
            )
        ]
    }


def resume_input(
    output: dict[str, Any],
    decision: OutcomeDecision,
    *,
    task: str,
    snapshot: str,
) -> dict[str, Any]:
    """Start a clean worker turn from trusted runtime evidence and model feedback.

    Prior assistant prose is intentionally excluded: only durable DeepAgents
    state plus the fresh browser snapshot and independent evaluator decision
    cross an outcome-iteration boundary.
    """
    state = {key: value for key, value in output.items() if key in {"todos", "files"}}
    state["messages"] = [
        HumanMessage(
            content=(
                "Continue the current job-application objective.\n\n"
                f"Requested task:\n{task}\n\n"
                "Independent application-outcome audit found missing, contradictory, "
                "or unproven criteria:\n\n"
                f"{decision.explanation}\n\n"
                "BEGIN UNTRUSTED CURRENT BROWSER EVIDENCE\n"
                f"{snapshot}\n"
                "END UNTRUSTED CURRENT BROWSER EVIDENCE\n\n"
                "You are the Orchestrator. You own application-flow decisions, "
                "coordination, and recovery. Decide and execute the next safe bounded "
                "specialist task yourself from current evidence. Do not print, simulate, "
                "or invent task calls or specialist results."
            )
        )
    ]
    return state


async def fresh_snapshot(browser_tools: Sequence[BaseTool], fallback: str) -> str:
    snapshot_tool = next(
        (tool for tool in browser_tools if tool.name == "browser_snapshot"),
        None,
    )
    if snapshot_tool is None:
        return fallback
    try:
        value = await snapshot_tool.ainvoke({})
    except Exception as exc:  # noqa: BLE001 - evaluator needs the current evidence failure
        return f"Snapshot unavailable: {exc}"
    return str(value or fallback)


def _evaluation_prompt(
    *,
    task: str,
    snapshot: str,
    application_state: ApplicationState | None,
) -> str:
    evidence = {
        "application_state": asdict(application_state) if application_state is not None else {},
        "latest_browser_snapshot": snapshot,
    }
    return f"""Audit the worker's complete observable execution against the outcome rubric.

Requested task:
{task}

Outcome rubric:
{APPLICATION_OUTCOME_RUBRIC}

Complete execution evidence:
{json.dumps(evidence, ensure_ascii=False, default=str)}

Call exactly one outcome transition tool. If any criterion lacks direct evidence, request revision
with concise audit findings. Use blocked only for a current dependency that prevents all
remaining safe progress.

Do not choose which specialist to call, whether to retry, or what application operation
comes next. Report only what is missing, contradictory, or unproven. The Orchestrator
owns recovery decisions.
"""


def append_tool_journal(
    journal: list[dict[str, Any]],
    stream_result: V3RunResult,
) -> None:
    trace = stream_result.output.get("_z_apply_tool_trace", ())
    if isinstance(trace, list):
        journal.extend(item for item in trace if isinstance(item, dict))
