from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from deepagents import SubAgent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.model_provider import ModelProvider
from z_apply_core.agents.no_progress_guard import NoProgressGuardMiddleware
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.retry_policy import model_retry_middleware
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.safe_tool_batch import SafeToolBatchMiddleware
from z_apply_core.agents.specialist_task_context import SpecialistTaskContextMiddleware
from z_apply_core.agents.specialists.answer_writer import build_answer_writer
from z_apply_core.agents.specialists.authentication import build_authentication_specialist
from z_apply_core.agents.specialists.page_analyst import build_page_analyst
from z_apply_core.agents.specialists.vision import build_vision_specialist
from z_apply_core.agents.vision_message_compat import VisionToolMessageCompatibilityMiddleware
from z_apply_core.stream_events import FrameworkEventSink


def _with_routing(
    spec: SubAgent,
    *,
    provider: ModelProvider,
    role: str,
    model: BaseChatModel,
    extra_middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
    preserve_task_context: bool = False,
    sink: FrameworkEventSink | None = None,
) -> SubAgent:
    enriched: dict[str, Any] = dict(spec)
    enriched["model"] = model
    router_middleware = NimRouterMiddleware(provider, role=role, sink=sink)
    enriched["middleware"] = [
        *extra_middleware,
        *([SpecialistTaskContextMiddleware()] if preserve_task_context else []),
        NoProgressGuardMiddleware(on_no_progress=router_middleware.reject_active_response),
        model_retry_middleware(),
        router_middleware,
        ProseToolCallGuardMiddleware(),
    ]
    return cast("SubAgent", enriched)


async def build_specialists(
    provider: ModelProvider,
    browser_tools: Sequence[BaseTool],
    *,
    fallback_model: BaseChatModel,
    candidate_resume: str = "",
    answer_writer_human_tools: Sequence[BaseTool] = (),
    answer_writer_memory_tools: Sequence[BaseTool] = (),
    answer_writer_middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
    authentication_tools: Sequence[BaseTool] = (),
    sink: FrameworkEventSink | None = None,
) -> list[SubAgent]:
    return [
        _with_routing(
            build_authentication_specialist(authentication_tools),
            provider=provider,
            role="AuthenticationSpecialist",
            model=fallback_model,
            extra_middleware=[
                SafeToolBatchMiddleware(),
                HumanEscalationGuardMiddleware(allowed_reasons=frozenset({"human_challenge"})),
            ],
            sink=sink,
        ),
        _with_routing(
            build_vision_specialist(browser_tools),
            provider=provider,
            role="VisionSpecialist",
            model=fallback_model,
            extra_middleware=[VisionToolMessageCompatibilityMiddleware()],
            sink=sink,
        ),
        _with_routing(
            build_page_analyst(),
            provider=provider,
            role="PageAnalyst",
            model=fallback_model,
            sink=sink,
        ),
        _with_routing(
            build_answer_writer(
                [*answer_writer_human_tools, *answer_writer_memory_tools],
                candidate_resume=candidate_resume,
            ),
            provider=provider,
            role="AnswerWriter",
            model=fallback_model,
            preserve_task_context=True,
            extra_middleware=answer_writer_middleware,
            sink=sink,
        ),
    ]
