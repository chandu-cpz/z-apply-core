from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

from deepagents import SubAgent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from z_apply_core.agents.authentication import AuthenticationBudgetMiddleware
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.middleware_factory import build_agent_middleware
from z_apply_core.agents.providers import ModelGateway
from z_apply_core.agents.router_middleware import build_router_middleware
from z_apply_core.agents.specialist_task_context import SpecialistTaskContextMiddleware
from z_apply_core.agents.specialists.answer_writer import build_answer_writer
from z_apply_core.agents.specialists.authentication import build_authentication_specialist
from z_apply_core.agents.specialists.submission_reviewer import (
    REVIEWER_BROWSER_TOOLS,
    build_submission_reviewer,
)
from z_apply_core.agents.specialists.vision import build_vision_specialist
from z_apply_core.agents.vision_message_compat import VisionToolMessageCompatibilityMiddleware
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.stream_events import FrameworkEventSink


def _with_routing(
    spec: SubAgent,
    *,
    provider: ModelGateway,
    role: str,
    model: BaseChatModel,
    extra_middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
    preserve_task_context: bool = False,
    sink: FrameworkEventSink | None = None,
    ledger: RunCallLedger | None = None,
    mutation_lock: asyncio.Lock | None = None,
) -> SubAgent:
    enriched: dict[str, Any] = dict(spec)
    enriched["model"] = model
    router_middleware = build_router_middleware(
        provider,
        role=role,
        sink=sink,
        ledger=ledger,
    )
    # Role-tuned NoProgress guard: browserless subagents need looser thresholds
    no_progress_kwargs: dict[str, Any] = {
        "max_identical_denials": 3,
        "max_non_progress": 5,
    }
    # extra_middleware lands after NoProgress/SerializeMutations in the shared
    # factory skeleton; SpecialistTaskContext only appends a reminder message
    # and no guard reads task identity from request messages, so the position
    # is safe.
    leading: list[AgentMiddleware[Any, Any, Any]] = []
    if preserve_task_context:
        leading.append(SpecialistTaskContextMiddleware())
    leading.extend(extra_middleware)
    enriched["middleware"] = build_agent_middleware(
        role=role,
        provider=provider,
        event_sink=sink,
        router_middleware=router_middleware,
        no_progress_kwargs=no_progress_kwargs,
        extra_middleware=leading,
        mutation_lock=mutation_lock,
    )
    return cast("SubAgent", enriched)


async def build_specialists(
    provider: ModelGateway,
    browser_tools: Sequence[BaseTool],
    *,
    fallback_model: BaseChatModel,
    candidate_resume: str = "",
    answer_writer_candidate_facts: Sequence[dict[str, object]] = (),
    answer_writer_human_tools: Sequence[BaseTool] = (),
    answer_writer_memory_tools: Sequence[BaseTool] = (),
    answer_writer_middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
    authentication_tools: Sequence[BaseTool] = (),
    submission_reviewer_tools: Sequence[BaseTool] = (),
    sink: FrameworkEventSink | None = None,
    mutation_lock: asyncio.Lock | None = None,
    ledger: RunCallLedger | None = None,
) -> list[SubAgent]:
    reviewer_tools: list[BaseTool] = [
        browser_tool
        for browser_tool in browser_tools
        if getattr(browser_tool, "name", "") in REVIEWER_BROWSER_TOOLS
    ]
    return [
        _with_routing(
            build_authentication_specialist(authentication_tools),
            provider=provider,
            role="AuthenticationSpecialist",
            model=fallback_model,
            ledger=ledger,
            extra_middleware=[
                HumanEscalationGuardMiddleware(allowed_reasons=frozenset({"human_challenge"})),
                AuthenticationBudgetMiddleware(max_waits=1),
            ],
            sink=sink,
            mutation_lock=mutation_lock,
        ),
        _with_routing(
            build_vision_specialist(browser_tools),
            provider=provider,
            role="VisionSpecialist",
            model=fallback_model,
            ledger=ledger,
            extra_middleware=[VisionToolMessageCompatibilityMiddleware()],
            sink=sink,
            mutation_lock=mutation_lock,
        ),
        _with_routing(
            build_answer_writer(
                [*answer_writer_human_tools, *answer_writer_memory_tools],
                candidate_resume=candidate_resume,
                candidate_facts=answer_writer_candidate_facts,
            ),
            provider=provider,
            role="AnswerWriter",
            model=fallback_model,
            ledger=ledger,
            preserve_task_context=True,
            extra_middleware=answer_writer_middleware,
            sink=sink,
            mutation_lock=mutation_lock,
        ),
        _with_routing(
            build_submission_reviewer([*reviewer_tools, *submission_reviewer_tools]),
            provider=provider,
            role="SubmissionReviewer",
            model=fallback_model,
            ledger=ledger,
            sink=sink,
            mutation_lock=mutation_lock,
        ),
    ]
