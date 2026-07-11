from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.router_middleware import NimRouterMiddleware
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
MAX_OUTCOME_VERDICT_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class OutcomeDecision:
    status: Literal["satisfied", "needs_revision", "blocked", "failed"]
    explanation: str
    next_action: str = ""


async def evaluate_application_outcome(
    *,
    task: str,
    output: dict[str, Any],
    tool_journal: Sequence[dict[str, Any]],
    snapshot: str,
    router: NimRouter,
    sink: FrameworkEventSink | None,
) -> OutcomeDecision:
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
    async def outcome_needs_revision(feedback: str, next_action: str) -> str:
        """Return evidence-based feedback and the next concrete operation to the worker."""
        nonlocal decision
        if decision is not None:
            return "An outcome transition has already been recorded for this evaluation."
        decision = OutcomeDecision("needs_revision", feedback, next_action)
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
        selection = await router.lease(tools=True, priority="quality", reasoning=True)
    except (NimRouterError, ImportError, ValueError) as exc:
        return OutcomeDecision("failed", f"Outcome evaluator model selection failed: {exc}")

    evaluator = create_deep_agent(
        model=selection.llm,
        tools=[outcome_satisfied, outcome_needs_revision, outcome_blocked],
        system_prompt=load_prompt("goal_evaluator.md"),
        middleware=[
            ModelRetryMiddleware(max_retries=3, on_failure="error"),
            NimRouterMiddleware(
                router,
                role="RecoveryAgent",
                initial_selection=selection,
            ),
        ],
    )
    prompt = _evaluation_prompt(
        task=task,
        output=output,
        tool_journal=tool_journal,
        snapshot=snapshot,
    )
    errors: list[str] = []
    for _attempt in range(1, MAX_OUTCOME_VERDICT_ATTEMPTS + 1):
        try:
            stream = evaluator.astream_events(
                {"messages": [{"role": "user", "content": prompt}]},
                version="v3",
            )
            await consume_deepagent_stream(stream, sink=sink, root_source="GoalEvaluator")
        except Exception as exc:  # noqa: BLE001 - report an explicit runtime failure
            errors.append(str(exc))
        if decision is not None:
            return decision

    detail = "; ".join(errors) if errors else "no outcome transition tool was called"
    return OutcomeDecision(
        "failed",
        (
            "Outcome evaluation failed to record a typed decision after "
            f"{MAX_OUTCOME_VERDICT_ATTEMPTS} attempts ({detail}). "
            "The runtime stopped safely without making another browser change."
        ),
    )


def resume_input(output: dict[str, Any], decision: OutcomeDecision) -> dict[str, Any]:
    state = {key: value for key, value in output.items() if key in {"messages", "todos", "files"}}
    messages = list(cast(Sequence[Any], state.get("messages", ())))
    messages.append(
        HumanMessage(
            content=(
                "Independent application-outcome evaluation: needs revision.\n\n"
                f"Evidence audit:\n{decision.explanation}\n\n"
                f"Next concrete action:\n{decision.next_action}\n\n"
                "Continue from the existing browser state. Perform the action through the "
                "appropriate specialist now; do not restate or redesign the whole plan."
            )
        )
    )
    state["messages"] = messages
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
    except Exception:  # noqa: BLE001 - evaluator still receives prior evidence
        return fallback
    return str(value or fallback)


def _evaluation_prompt(
    *,
    task: str,
    output: dict[str, Any],
    tool_journal: Sequence[dict[str, Any]],
    snapshot: str,
) -> str:
    evidence = {
        "messages": [_message_record(message) for message in output.get("messages", ())],
        "todos": output.get("todos", []),
        "tool_journal": list(tool_journal),
        "current_browser_snapshot": snapshot,
    }
    return f"""Evaluate the worker's complete observable execution against the automatic outcome.

Requested task:
{task}

Outcome rubric:
{APPLICATION_OUTCOME_RUBRIC}

Complete execution evidence:
{json.dumps(evidence, ensure_ascii=False, default=str)}

Call exactly one outcome transition tool. If any criterion lacks direct evidence, request revision
and name the next concrete operation. Use blocked only for a current dependency that prevents all
remaining safe progress.
"""


def _message_record(message: Any) -> dict[str, Any]:
    return {
        "type": type(message).__name__,
        "name": getattr(message, "name", None),
        "content": getattr(message, "content", ""),
        "tool_calls": getattr(message, "tool_calls", None),
        "tool_call_id": getattr(message, "tool_call_id", None),
    }


def append_tool_journal(
    journal: list[dict[str, Any]],
    stream_result: V3RunResult,
) -> None:
    trace = stream_result.output.get("_z_apply_tool_trace", ())
    if isinstance(trace, list):
        journal.extend(item for item in trace if isinstance(item, dict))
