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
from nim_router.errors import ErrorKind
from nim_router.schemas import ModelSelection

from z_apply_core.agents.protocol_guard import ToolProtocolViolation

logger = logging.getLogger(__name__)

# Role → routing policy.
# ``reasoning`` / ``priority`` are role-based; ``force_vision`` always treats
# the request as needing a vision-capable model.
ROLE_POLICY: dict[str, dict[str, Any]] = {
    "orchestrator": {"priority": "balanced", "reasoning": False},
    "auth_orchestrator": {"priority": "balanced", "reasoning": False},
    "BrowserSpecialist": {"priority": "balanced", "reasoning": False},
    "FieldMapper": {"priority": "balanced", "reasoning": False},
    "AnswerWriter": {"priority": "quality", "reasoning": False},
    "Verifier": {"priority": "balanced", "reasoning": False},
    "auth_verifier": {"priority": "quality", "reasoning": True},
    "VisionSpecialist": {"priority": "balanced", "reasoning": False, "force_vision": True},
    "RecoveryAgent": {"priority": "balanced", "reasoning": False},
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

    def __init__(
        self,
        router: NimRouter,
        role: str,
        *,
        initial_selection: ModelSelection | None = None,
    ) -> None:
        super().__init__()
        self._router = router
        self._role = role
        self._policy = ROLE_POLICY.get(role, {"priority": "balanced", "reasoning": False})
        self._initial_selection = initial_selection
        self._last_model_id = initial_selection.info.id if initial_selection is not None else ""

    @property
    def role(self) -> str:
        return self._role

    @property
    def last_model_id(self) -> str:
        return self._last_model_id

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

        logger.debug(
            "NimRouterMiddleware.awrap_model_call "
            "role=%s tools=%s structured=%s vision=%s reasoning=%s priority=%s",
            self._role,
            tools,
            structured,
            vision,
            reasoning,
            priority,
        )

        selection = self._initial_selection
        self._initial_selection = None
        if selection is None:
            selection = await self._router.lease(
                tools=tools,
                structured=structured,
                vision=vision,
                reasoning=reasoning,
                priority=priority,
            )
        self._last_model_id = selection.info.id

        logger.debug(
            "NimRouterMiddleware leased model=%s for role=%s",
            selection.info.id,
            self._role,
        )

        start = time.monotonic()
        try:
            leased_model: BaseChatModel = selection.llm
            result: ModelResponse[ResponseT] = await handler(request.override(model=leased_model))
        except BaseException as exc:  # noqa: BLE001 - re-raised after recording
            logger.debug(
                "NimRouterMiddleware recording failure for model=%s role=%s",
                selection.info.id,
                self._role,
            )
            protocol_failure = isinstance(exc, ToolProtocolViolation)
            self._router.record_failure(
                selection.info.id,
                error=exc,
                kind=ErrorKind.TOOL_CALL_FAILURE if protocol_failure else None,
                tools=tools,
                structured=structured,
                vision=vision,
            )
            if protocol_failure:
                logger.warning(
                    "NimRouterMiddleware recorded tool protocol failure for model=%s role=%s",
                    selection.info.id,
                    self._role,
                )
            raise
        else:
            latency = time.monotonic() - start
            logger.debug(
                "NimRouterMiddleware recording success for model=%s role=%s latency=%.2f",
                selection.info.id,
                self._role,
                latency,
            )
            self._router.record_success(
                selection.info.id,
                latency=latency,
                tools=tools,
                structured=structured,
                vision=vision,
            )
            return result
