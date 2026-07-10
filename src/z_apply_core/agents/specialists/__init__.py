from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from deepagents import SubAgent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from nim_router import NimRouter

from z_apply_core.agents.browser_action_verification import BrowserActionVerificationMiddleware
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.specialists.answer_writer import build_answer_writer
from z_apply_core.agents.specialists.browser import build_browser_specialist
from z_apply_core.agents.specialists.field_mapper import build_field_mapper
from z_apply_core.agents.specialists.verifier import build_verifier
from z_apply_core.agents.specialists.vision import build_vision_specialist
from z_apply_core.browser_tools import VERIFIER_BROWSER_TOOLS


async def _default_model(router: NimRouter) -> BaseChatModel:
    selection = await router.select(tools=True, priority="balanced")
    model: BaseChatModel = selection.llm
    return model


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
    ]
    return cast("SubAgent", enriched)


async def build_specialists(
    router: NimRouter,
    browser_tools: Sequence[BaseTool],
) -> list[SubAgent]:
    read_only_browser_tools = [
        tool for tool in browser_tools if tool.name in VERIFIER_BROWSER_TOOLS
    ]
    model = await _default_model(router)
    browser_verification = BrowserActionVerificationMiddleware(
        fallback_model=model,
        router=router,
        read_only_browser_tools=read_only_browser_tools,
        prompt_name="verifier.md",
        verifier_role="Verifier",
    )
    return [
        _with_routing(
            build_browser_specialist(browser_tools),
            router=router,
            role="BrowserSpecialist",
            model=model,
            extra_middleware=[browser_verification],
        ),
        _with_routing(
            build_vision_specialist(browser_tools),
            router=router,
            role="VisionSpecialist",
            model=model,
        ),
        _with_routing(
            build_field_mapper(read_only_browser_tools),
            router=router,
            role="FieldMapper",
            model=model,
        ),
        _with_routing(
            build_answer_writer(),
            router=router,
            role="AnswerWriter",
            model=model,
        ),
        _with_routing(
            build_verifier(read_only_browser_tools),
            router=router,
            role="Verifier",
            model=model,
        ),
    ]


async def build_auth_specialists(
    router: NimRouter,
    browser_tools: Sequence[BaseTool],
) -> list[SubAgent]:
    read_only_browser_tools = [
        tool for tool in browser_tools if tool.name in VERIFIER_BROWSER_TOOLS
    ]
    model = await _default_model(router)
    browser_verification = BrowserActionVerificationMiddleware(
        fallback_model=model,
        router=router,
        read_only_browser_tools=read_only_browser_tools,
        prompt_name="auth_verifier.md",
        verifier_role="auth_verifier",
    )
    return [
        _with_routing(
            build_browser_specialist(
                browser_tools,
                prompt_name="auth_browser_specialist.md",
            ),
            router=router,
            role="BrowserSpecialist",
            model=model,
            extra_middleware=[browser_verification],
        ),
        _with_routing(
            build_verifier(
                read_only_browser_tools,
                prompt_name="auth_verifier.md",
            ),
            router=router,
            role="auth_verifier",
            model=model,
        ),
    ]
