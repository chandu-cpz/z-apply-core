from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any, Literal, cast

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ModelResponse,
    ResponseT,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AnyMessage
from nim_router import NimRouter
from nim_router.schemas import ModelSelection

logger = logging.getLogger(__name__)

# Role → routing policy.
# ``reasoning`` / ``priority`` are role-based; ``force_vision`` always treats
# the request as needing a vision-capable model.
ROLE_POLICY: dict[str, dict[str, Any]] = {
    "orchestrator": {"priority": "balanced", "reasoning": False},
    "auth_orchestrator": {"priority": "balanced", "reasoning": False},
    "BrowserSpecialist": {"priority": "fast", "reasoning": False},
    "FieldMapper": {"priority": "balanced", "reasoning": False},
    "AnswerWriter": {"priority": "quality", "reasoning": False},
    "Verifier": {"priority": "quality", "reasoning": True},
    "auth_verifier": {"priority": "quality", "reasoning": True},
    "VisionSpecialist": {"priority": "balanced", "reasoning": False, "force_vision": True},
    "RecoveryAgent": {"priority": "quality", "reasoning": True},
}


def _detect_vision(messages: Sequence[AnyMessage]) -> bool:
    """Return True if any message carries an image content block."""
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"image", "image_url"}:
                return True
    return False


class NimRouterMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Route every individual DeepAgents model call through ``NimRouter``.

    Inspects the :class:`ModelRequest` to infer capabilities (tools,
    structured output, vision) and applies a role-based policy (priority,
    reasoning). It leases a model from the router for this single call only,
    overrides the request model, invokes the handler, then records
    success/failure back to the router manually (the request does not expose
    runnable config, so no callback is injected).
    """

    def __init__(self, router: NimRouter, role: str) -> None:
        super().__init__()
        self._router = router
        self._role = role
        self._policy = ROLE_POLICY.get(role, {"priority": "balanced", "reasoning": False})

    @property
    def role(self) -> str:
        return self._role

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> Any:
        tools = bool(request.tools)
        structured = request.response_format is not None
        vision = _detect_vision(request.messages) or bool(self._policy.get("force_vision"))
        reasoning = bool(self._policy.get("reasoning", False))
        priority = cast(
            "Literal['fast', 'quality', 'balanced']",
            self._policy.get("priority", "balanced"),
        )

        selection: ModelSelection = await self._router.lease(
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
            priority=priority,
        )

        start = time.monotonic()
        try:
            leased_model: BaseChatModel = selection.llm
            result: ModelResponse[ResponseT] = await handler(
                request.override(model=leased_model)
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised after recording
            self._router.record_failure(
                selection.info.id,
                error=exc,
                tools=tools,
                structured=structured,
                vision=vision,
            )
            raise
        else:
            self._router.record_success(
                selection.info.id,
                latency=time.monotonic() - start,
                tools=tools,
                structured=structured,
                vision=vision,
            )
            return result
