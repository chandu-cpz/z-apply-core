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
from langchain_core.messages import AIMessage, AnyMessage
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
    "AnswerWriter": {"priority": "quality", "reasoning": False},
    "VisionSpecialist": {"priority": "balanced", "reasoning": False, "force_vision": True},
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


def _restore_empty_content_from_reasoning(response: Any) -> Any:
    """Preserve a provider's final answer when it only populated reasoning_content."""
    messages = getattr(response, "result", None)
    if not isinstance(messages, list):
        return response

    changed = False
    normalized: list[Any] = []
    for message in messages:
        if not isinstance(message, AIMessage) or message.tool_calls or message.text.strip():
            normalized.append(message)
            continue
        reasoning = message.additional_kwargs.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning.strip():
            normalized.append(message)
            continue
        normalized.append(message.model_copy(update={"content": reasoning.strip()}))
        changed = True

    if not changed:
        return response
    logger.info("Recovered empty assistant content from provider reasoning_content")
    return ModelResponse(
        result=normalized,
        structured_response=response.structured_response,
    )


class NimRouterMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Keep one routed model sticky until a model call actually fails.

    Inspects the :class:`ModelRequest` to infer capabilities (tools,
    structured output, vision) and applies a role-based policy (priority,
    reasoning). A successful lease is reused through the agent's tool loop.
    Provider, rate-limit, and protocol failures release it so outer retry
    middleware can lease another eligible model without switching models
    during healthy execution.
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
        self._active_selection = initial_selection
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

        selection = self._active_selection
        if selection is None:
            selection = await self._router.lease(
                tools=tools,
                structured=structured,
                vision=vision,
                reasoning=reasoning,
                priority=priority,
            )
            self._active_selection = selection
        _attach_tracking_callback(selection)
        self._last_model_id = selection.info.id

        logger.info(
            "router %s selected %s (priority=%s, tools=%s, structured=%s, "
            "vision=%s, reasoning=%s, candidates=%d, exploring=%s)",
            self._role,
            selection.info.id,
            priority,
            tools,
            structured,
            vision,
            reasoning,
            len(self._router._candidates) if hasattr(self._router, "_candidates") else 0,
            bool(self._router._exploring) if hasattr(self._router, "_exploring") else False,
        )

        start = time.monotonic()
        try:
            leased_model: BaseChatModel = selection.llm
            result: ModelResponse[ResponseT] = await handler(request.override(model=leased_model))
        except BaseException as exc:  # noqa: BLE001 - re-raised after recording
            logger.warning(
                "router %s model %s failed: %s",
                self._role,
                selection.info.id,
                exc,
            )
            protocol_failure = isinstance(exc, ToolProtocolViolation)
            if protocol_failure:
                self._router.record_failure(
                    selection.info.id,
                    error=exc,
                    kind=ErrorKind.TOOL_CALL_FAILURE,
                    tools=tools,
                    structured=structured,
                    vision=vision,
                )
                self._router.cooldown_model(selection.info.id, 20.0)
            self._active_selection = None
            if protocol_failure:
                logger.warning(
                    "NimRouterMiddleware recorded tool protocol failure for model=%s role=%s",
                    selection.info.id,
                    self._role,
                )
            raise
        else:
            latency = time.monotonic() - start
            logger.info(
                "router %s model %s succeeded in %.2fs",
                self._role,
                selection.info.id,
                latency,
            )
            return _restore_empty_content_from_reasoning(result)


def _attach_tracking_callback(selection: ModelSelection) -> None:
    """Attach the lease callback once so every sticky invocation is RPM-accounted."""
    callback = getattr(selection, "callback", None)
    if callback is None:
        return
    callbacks = list(selection.llm.callbacks or [])
    if callback not in callbacks:
        selection.llm.callbacks = [*callbacks, callback]
