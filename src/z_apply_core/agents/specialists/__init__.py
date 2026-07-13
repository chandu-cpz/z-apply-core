from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from deepagents import SubAgent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from nim_router import NimRouter

from z_apply_core.agents.duplicate_mutation_guard import DuplicateMutationGuardMiddleware
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.retry_policy import model_retry_middleware
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.specialists.answer_writer import build_answer_writer
from z_apply_core.agents.specialists.browser import build_browser_specialist
from z_apply_core.agents.specialists.vision import build_vision_specialist
from z_apply_core.memory.applicant_memory import CandidateMemory, build_answer_writer_memory_tools


def _with_routing(
    spec: SubAgent,
    *,
    router: NimRouter,
    role: str,
    model: BaseChatModel,
    extra_middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
) -> SubAgent:
    enriched: dict[str, Any] = dict(spec)
    enriched["model"] = model
    enriched["middleware"] = [
        *extra_middleware,
        model_retry_middleware(),
        NimRouterMiddleware(router, role=role),
        ProseToolCallGuardMiddleware(),
    ]
    return cast("SubAgent", enriched)


async def build_specialists(
    router: NimRouter,
    browser_tools: Sequence[BaseTool],
    *,
    fallback_model: BaseChatModel,
    candidate_memory: CandidateMemory | None = None,
    answer_writer_human_tools: Sequence[BaseTool] = (),
    answer_writer_middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
) -> list[SubAgent]:
    return [
        _with_routing(
            build_browser_specialist(browser_tools),
            router=router,
            role="BrowserSpecialist",
            model=fallback_model,
            extra_middleware=[DuplicateMutationGuardMiddleware()],
        ),
        _with_routing(
            build_vision_specialist(browser_tools),
            router=router,
            role="VisionSpecialist",
            model=fallback_model,
        ),
        _with_routing(
            build_answer_writer(
                [
                    *build_answer_writer_memory_tools(candidate_memory),
                    *answer_writer_human_tools,
                ]
            ),
            router=router,
            role="AnswerWriter",
            model=fallback_model,
            extra_middleware=answer_writer_middleware,
        ),
    ]
