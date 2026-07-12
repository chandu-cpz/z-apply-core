from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from deepagents import SubAgent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from nim_router import NimRouter

from z_apply_core.agents.application_progress import (
    ApplicationProgress,
    BrowserUploadProgressMiddleware,
    make_field_map_tools,
)
from z_apply_core.agents.browser_action_verification import BrowserActionVerificationMiddleware
from z_apply_core.agents.duplicate_mutation_guard import DuplicateMutationGuardMiddleware
from z_apply_core.agents.protocol_guard import ProseToolCallGuardMiddleware
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.specialists.answer_writer import build_answer_writer
from z_apply_core.agents.specialists.browser import build_browser_specialist
from z_apply_core.agents.specialists.field_mapper import build_field_mapper
from z_apply_core.agents.specialists.verifier import build_verifier
from z_apply_core.agents.specialists.vision import build_vision_specialist
from z_apply_core.browser_tools import VERIFIER_BROWSER_TOOLS
from z_apply_core.memory.applicant_memory import CandidateMemory, build_answer_writer_memory_tools
from z_apply_core.stream_events import FrameworkEventSink


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
        ModelRetryMiddleware(max_retries=2, on_failure="error"),
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
    progress: ApplicationProgress | None = None,
) -> list[SubAgent]:
    read_only_browser_tools = [
        tool for tool in browser_tools if tool.name in VERIFIER_BROWSER_TOOLS
    ]
    return [
        _with_routing(
            build_browser_specialist(browser_tools),
            router=router,
            role="BrowserSpecialist",
            model=fallback_model,
            extra_middleware=[
                DuplicateMutationGuardMiddleware(),
                *([BrowserUploadProgressMiddleware(progress)] if progress is not None else []),
            ],
        ),
        _with_routing(
            build_vision_specialist(browser_tools),
            router=router,
            role="VisionSpecialist",
            model=fallback_model,
        ),
        _with_routing(
            build_field_mapper(
                read_only_browser_tools,
                state_tools=make_field_map_tools(progress) if progress is not None else (),
            ),
            router=router,
            role="FieldMapper",
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
        _with_routing(
            build_verifier(read_only_browser_tools),
            router=router,
            role="Verifier",
            model=fallback_model,
        ),
    ]


async def build_auth_specialists(
    router: NimRouter,
    browser_tools: Sequence[BaseTool],
    *,
    fallback_model: BaseChatModel,
    sink: FrameworkEventSink | None = None,
) -> list[SubAgent]:
    read_only_browser_tools = [
        tool for tool in browser_tools if tool.name in VERIFIER_BROWSER_TOOLS
    ]
    browser_verification = BrowserActionVerificationMiddleware(
        fallback_model=fallback_model,
        router=router,
        read_only_browser_tools=read_only_browser_tools,
        prompt_name="auth_verifier.md",
        verifier_role="auth_verifier",
        sink=sink,
    )
    return [
        _with_routing(
            build_browser_specialist(
                browser_tools,
                prompt_name="auth_browser_specialist.md",
            ),
            router=router,
            role="BrowserSpecialist",
            model=fallback_model,
            extra_middleware=[browser_verification],
        ),
        _with_routing(
            build_verifier(
                read_only_browser_tools,
                prompt_name="auth_verifier.md",
            ),
            router=router,
            role="auth_verifier",
            model=fallback_model,
        ),
    ]
