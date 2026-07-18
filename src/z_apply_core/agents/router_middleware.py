from __future__ import annotations

import asyncio
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
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from nim_router import NimRouter
from nim_router.errors import ErrorKind
from nim_router.schemas import ModelSelection

from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

logger = logging.getLogger(__name__)

ORCHESTRATOR_EXCLUDED_MODEL_IDS: frozenset[str] = frozenset(
    {
        "nvidia/nemotron-3-super-120b-a12b",
        "openai/gpt-oss-120b",
    }
)

# Role → routing policy.
# ``reasoning`` / ``priority`` are role-based; ``force_vision`` always treats
# the request as needing a vision-capable model.
ROLE_POLICY: dict[str, dict[str, Any]] = {
    "orchestrator": {
        "priority": "balanced",
        "reasoning": True,
        "excluded_model_ids": ORCHESTRATOR_EXCLUDED_MODEL_IDS,
    },
    "auth_orchestrator": {
        "priority": "balanced",
        "reasoning": True,
        "excluded_model_ids": ORCHESTRATOR_EXCLUDED_MODEL_IDS,
    },
    "AuthenticationSpecialist": {
        "priority": "balanced",
        "reasoning": True,
        "excluded_model_ids": ORCHESTRATOR_EXCLUDED_MODEL_IDS,
    },
    "BrowserSpecialist": {"priority": "balanced", "reasoning": True},
    "AnswerWriter": {"priority": "quality", "reasoning": True},
    "VisionSpecialist": {"priority": "balanced", "reasoning": True, "force_vision": True},
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


def _normalize_provider_reasoning(response: Any) -> tuple[Any, bool]:
    """Keep provider reasoning out of assistant content and detect missing finals."""
    messages = getattr(response, "result", None)
    if not isinstance(messages, list):
        return response, False

    changed = False
    missing_final = False
    normalized: list[Any] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            normalized.append(message)
            continue

        content = message.content
        if isinstance(content, str) and "</think>" in content:
            final_content = content.rpartition("</think>")[2].strip()
            normalized.append(message.model_copy(update={"content": final_content}))
            changed = True
            if not final_content and not message.tool_calls:
                missing_final = True
            continue

        if message.tool_calls or message.text.strip():
            normalized.append(message)
            continue

        normalized.append(message)
        missing_final = True

    if not changed:
        return response, missing_final
    logger.info("Removed provider reasoning tags from assistant content")
    return (
        ModelResponse(
            result=normalized,
            structured_response=response.structured_response,
        ),
        missing_final,
    )


def _drop_orphan_tool_messages(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """Preserve only tool results adjacent to their structured assistant call."""
    pending_tool_call_ids: set[str] = set()
    normalized: list[AnyMessage] = []
    removed = 0
    for message in messages:
        if isinstance(message, AIMessage):
            pending_tool_call_ids = {
                call_id
                for call in message.tool_calls
                if isinstance((call_id := call.get("id")), str) and call_id
            }
            normalized.append(message)
            continue
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending_tool_call_ids:
                removed += 1
                continue
            pending_tool_call_ids.remove(message.tool_call_id)
            normalized.append(message)
            continue
        pending_tool_call_ids.clear()
        normalized.append(message)
    if removed:
        logger.warning("Removed %d orphan tool result message(s) before model handoff", removed)
    return normalized


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
        sink: FrameworkEventSink | None = None,
    ) -> None:
        super().__init__()
        self._router = router
        self._role = role
        self._policy = ROLE_POLICY.get(role, {"priority": "balanced", "reasoning": True})
        self._active_selection = initial_selection
        self._last_model_id = initial_selection.info.id if initial_selection is not None else ""
        self._sink = sink
        self._selection_announced = False

    @property
    def role(self) -> str:
        return self._role

    @property
    def last_model_id(self) -> str:
        return self._last_model_id

    def reject_active_response(self, error: ToolProtocolViolation) -> None:
        """Record a semantically unusable response and rotate the sticky lease."""
        selection = self._active_selection
        if selection is None:
            return
        self._router.record_failure(
            selection.info.id,
            error=error,
            kind=ErrorKind.TOOL_CALL_FAILURE,
            tools=True,
            structured=False,
            vision=bool(self._policy.get("force_vision")),
        )
        self._router.cooldown_model(selection.info.id, 20.0)
        logger.warning(
            "router %s rejected no-progress response from %s and released sticky lease",
            self._role,
            selection.info.id,
        )
        self._active_selection = None
        self._selection_announced = False
        self._emit_from_sync(
            "model_rotated",
            selection.info.id,
            {"reason": "tool_protocol_failure"},
        )

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
        leased_new = selection is None
        if selection is None:
            lease_kwargs: dict[str, Any] = {}
            excluded_model_ids = self._policy.get("excluded_model_ids")
            if excluded_model_ids:
                lease_kwargs["excluded_model_ids"] = excluded_model_ids
            selection = await self._router.lease(
                tools=tools,
                structured=structured,
                vision=vision,
                reasoning=reasoning,
                priority=priority,
                **lease_kwargs,
            )
            self._active_selection = selection
        _attach_tracking_callback(selection)
        self._last_model_id = selection.info.id
        if leased_new or not self._selection_announced:
            await self._emit(
                "model_selected",
                selection.info.id,
                {
                    "role": self._role,
                    "priority": priority,
                    "tools": tools,
                    "structured": structured,
                    "vision": vision,
                    "reasoning": reasoning,
                },
            )
            self._selection_announced = True

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
            len(cast(Sequence[Any], getattr(self._router, "_candidates", ()))),
            bool(self._router._exploring) if hasattr(self._router, "_exploring") else False,
        )

        start = time.monotonic()
        try:
            leased_model: BaseChatModel = selection.llm
            sanitized_messages = _drop_orphan_tool_messages(request.messages)
            override: dict[str, Any] = {"model": leased_model}
            if len(sanitized_messages) != len(request.messages):
                override["messages"] = sanitized_messages
            timeout_seconds = _model_call_timeout_seconds(self._router)
            async with asyncio.timeout(timeout_seconds):
                result: ModelResponse[ResponseT] = await handler(
                    request.override(**override)
                )
        except BaseException as exc:  # noqa: BLE001 - re-raised after recording
            logger.warning(
                "router %s model %s failed: %s",
                self._role,
                selection.info.id,
                exc,
            )
            protocol_failure = isinstance(exc, ToolProtocolViolation)
            if isinstance(exc, TimeoutError):
                self._router.record_failure(
                    selection.info.id,
                    error=exc,
                    kind=ErrorKind.TIMEOUT,
                    tools=tools,
                    structured=structured,
                    vision=vision,
                )
            elif protocol_failure:
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
            self._selection_announced = False
            await self._emit(
                "model_failed",
                selection.info.id,
                {
                    "role": self._role,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            await self._emit(
                "model_rotated",
                selection.info.id,
                {"role": self._role, "reason": "model_call_failed"},
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
            result, missing_final = _normalize_provider_reasoning(result)
            if missing_final:
                failure = ToolProtocolViolation(
                    "tool_protocol_failure: provider returned reasoning without a "
                    "native tool call or final assistant answer"
                )
                self._router.record_failure(
                    selection.info.id,
                    error=failure,
                    kind=ErrorKind.TOOL_CALL_FAILURE,
                    tools=tools,
                    structured=structured,
                    vision=vision,
                )
                self._router.cooldown_model(selection.info.id, 20.0)
                self._active_selection = None
                self._selection_announced = False
                await self._emit(
                    "model_failed",
                    selection.info.id,
                    {
                        "role": self._role,
                        "error_type": type(failure).__name__,
                        "error": str(failure),
                    },
                )
                await self._emit(
                    "model_rotated",
                    selection.info.id,
                    {"role": self._role, "reason": "missing_native_action"},
                )
                raise failure
            logger.info(
                "router %s model %s succeeded in %.2fs",
                self._role,
                selection.info.id,
                latency,
            )
            return result

    async def _emit(self, event: str, model_id: str, data: dict[str, Any]) -> None:
        if self._sink is None:
            return
        await self._sink.accept(
            FrameworkTraceEvent(
                event=event,
                name=self._role,
                data={"model_id": model_id, **data},
                raw={},
            )
        )

    def _emit_from_sync(self, event: str, model_id: str, data: dict[str, Any]) -> None:
        if self._sink is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._emit(event, model_id, {"role": self._role, **data}))


def _model_call_timeout_seconds(router: NimRouter) -> float | None:
    value = getattr(getattr(router, "config", None), "timeout_seconds", None)
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return None


def _attach_tracking_callback(selection: ModelSelection) -> None:
    """Attach the lease callback once so every sticky invocation is RPM-accounted."""
    callback = getattr(selection, "callback", None)
    if callback is None:
        return
    callback = cast(BaseCallbackHandler, callback)
    configured_callbacks = selection.llm.callbacks
    callbacks: list[BaseCallbackHandler]
    if isinstance(configured_callbacks, list):
        callbacks = [cast(BaseCallbackHandler, item) for item in configured_callbacks]
    else:
        callbacks = list(
            cast(Sequence[BaseCallbackHandler], getattr(configured_callbacks, "handlers", ()))
        )
    if callback not in callbacks:
        selection.llm.callbacks = [*callbacks, callback]
