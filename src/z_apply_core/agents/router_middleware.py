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
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
)
from nim_router.errors import ErrorKind
from nim_router.schemas import ModelSelection

from z_apply_core.agents.model_provider import ModelProvider
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.context.call_ledger import RunCallLedger
from z_apply_core.context.model_metrics import (
    CallContent,
    CallMetrics,
    MetricStream,
    begin_model_call,
    content_from_messages,
    extract_cache_read,
    extract_cost,
    extract_usage_dict,
    extract_usage_tokens,
    format_call_metrics,
    is_async_iterable,
    last_call_timing,
)
from z_apply_core.context.token_metric import estimate_messages_tokens
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
    """Detect assistant responses that produced no final content or tool call.

    Provider reasoning arrives out-of-band via ``reasoning_content`` and never
    enters assistant content, so no content rewriting happens here; the only
    concern is flagging a response that is all reasoning with no final message.
    """
    messages = getattr(response, "result", None)
    if not isinstance(messages, list):
        return response, False

    missing_final = any(
        isinstance(message, AIMessage) and not message.tool_calls and not message.text.strip()
        for message in messages
    )
    if not missing_final:
        return response, False
    return (
        ModelResponse(
            result=messages,
            structured_response=response.structured_response,
        ),
        True,
    )


def _prompt_preview(messages: Sequence[AnyMessage], limit: int = 400) -> str:
    """Return a short preview of the latest substantive user/system content.

    Lets the terminal show what the model was asked even when the graph's
    stream projections are silent (recovery re-entries, subagents).
    """
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return _squash(content, limit)
        if isinstance(content, list):
            parts = [
                block.get("text")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            text = " ".join(part for part in parts if part).strip()
            if text:
                return _squash(text, limit)
    return ""


def _squash(value: str, limit: int) -> str:
    preview = " ".join(value.split())
    return preview[:limit] if len(preview) > limit else preview


_CONTENT_TEXT_LIMIT = 8_000
_REASONING_LIMIT = 4_000
_TOOL_ARGS_LIMIT = 2_000


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _report_call(
    sink: FrameworkEventSink | None,
    *,
    role: str,
    model_id: str,
    provider: str,
    metrics: CallMetrics,
    content: CallContent,
    prompt_preview: str,
    ledger: RunCallLedger | None = None,
    input_tokens_estimate: int | None = None,
) -> None:
    """Emit the terminal-visible record of one completed model call.

    ``model_call_metrics`` keeps its historical shape (backend consumers),
    while ``model_call_content`` carries the model's reasoning, text, and tool
    calls so the Rich stream shows every LLM call in every phase.
    """
    common = {
        "role": role,
        "provider": provider,
        "duration_ms": metrics.duration_ms,
        "ttft_ms": metrics.ttft_ms,
        "input_tokens": metrics.input_tokens,
        "output_tokens": metrics.output_tokens,
        "cache_read_tokens": metrics.cache_read_tokens,
    }
    tool_calls = [
        {
            **call,
            "args": _clip(str(call.get("args") or ""), _TOOL_ARGS_LIMIT),
        }
        if isinstance(call, dict)
        else call
        for call in content.tool_calls
    ]
    _emit_router_event_sync(
        sink,
        role,
        "model_call_metrics",
        model_id,
        common,
    )
    _emit_router_event_sync(
        sink,
        role,
        "model_call_content",
        model_id,
        {
            **common,
            "text": _clip(content.text, _CONTENT_TEXT_LIMIT),
            "reasoning": _clip(content.reasoning, _REASONING_LIMIT),
            "tool_calls": tool_calls,
            "prompt_preview": prompt_preview,
        },
    )

    if ledger is not None:
        input_tokens = (
            metrics.input_tokens
            if metrics.input_tokens is not None
            else (input_tokens_estimate if input_tokens_estimate is not None else 0)
        )
        output_tokens = metrics.output_tokens if metrics.output_tokens is not None else 0
        ledger.record(
            agent=role,
            model_id=model_id,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=metrics.cache_read_tokens or 0,
            ttft_ms=metrics.ttft_ms,
            duration_ms=metrics.duration_ms,
            gateway_cost_usd=metrics.cost_usd,
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
        provider: ModelProvider,
        role: str,
        *,
        initial_selection: ModelSelection | None = None,
        sink: FrameworkEventSink | None = None,
        ledger: RunCallLedger | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._role = role
        self._policy = ROLE_POLICY.get(role, {"priority": "balanced", "reasoning": True})
        self._active_selection = initial_selection
        self._last_model_id = initial_selection.info.id if initial_selection is not None else ""
        self._sink = sink
        self._ledger = ledger
        self._selection_announced = False

    @property
    def role(self) -> str:
        return self._role

    @property
    def name(self) -> str:
        return f"NimRouterMiddleware[{self._role}]"

    @property
    def last_model_id(self) -> str:
        return self._last_model_id

    def reject_active_response(self, error: ToolProtocolViolation) -> None:
        """Record a semantically unusable response and rotate the sticky lease."""
        selection = self._active_selection
        if selection is None:
            return
        self._provider.record_failure(
            selection.info.id,
            error=error,
            kind=ErrorKind.TOOL_CALL_FAILURE,
            tools=True,
            structured=False,
            vision=bool(self._policy.get("force_vision")),
        )
        self._provider.cooldown_model(selection.info.id, 20.0)
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
            excluded_model_ids = self._policy.get("excluded_model_ids")
            selection = await self._provider.lease(
                tools=tools,
                structured=structured,
                vision=vision,
                reasoning=reasoning,
                priority=priority,
                excluded_model_ids=excluded_model_ids,
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
            "router %s selected %s (priority=%s, tools=%s, structured=%s, vision=%s, reasoning=%s)",
            self._role,
            selection.info.id,
            priority,
            tools,
            structured,
            vision,
            reasoning,
        )

        info_metadata = getattr(selection.info, "metadata", None) or {}
        provider = str(info_metadata.get("provider") or "?")
        prompt_preview = _prompt_preview(request.messages)
        input_tokens_estimate = estimate_messages_tokens(request.messages)
        await self._emit(
            "model_call_start",
            selection.info.id,
            {
                "role": self._role,
                "provider": provider,
                "input_tokens_estimate": input_tokens_estimate,
                "tool_count": len(request.tools or []),
                "prompt_preview": prompt_preview,
            },
        )

        start = time.monotonic()
        begin_model_call()
        try:
            leased_model: BaseChatModel = selection.llm
            # Orphan tool results stay in framework history on purpose: any
            # mid-conversation deletion changes the byte prefix the gateway's
            # prompt cache keys on, so every later turn re-misses at the full
            # input price instead of cheap cache reads. Orphans are filtered
            # at the wire in the provider payload builder instead.
            override: dict[str, Any] = {"model": leased_model}
            timeout_seconds = _model_call_timeout_seconds(self._provider)
            async with asyncio.timeout(timeout_seconds):
                result: ModelResponse[ResponseT] = await handler(request.override(**override))
        except BaseException as exc:  # noqa: BLE001 - re-raised after recording
            logger.warning(
                "router %s model %s failed: %s",
                self._role,
                selection.info.id,
                exc,
            )
            protocol_failure = isinstance(exc, ToolProtocolViolation)
            if isinstance(exc, TimeoutError):
                self._provider.record_failure(
                    selection.info.id,
                    error=exc,
                    kind=ErrorKind.TIMEOUT,
                    tools=tools,
                    structured=structured,
                    vision=vision,
                )
            elif protocol_failure:
                self._provider.record_failure(
                    selection.info.id,
                    error=exc,
                    kind=ErrorKind.TOOL_CALL_FAILURE,
                    tools=tools,
                    structured=structured,
                    vision=vision,
                )
                self._provider.cooldown_model(selection.info.id, 20.0)
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
                self._provider.record_failure(
                    selection.info.id,
                    error=failure,
                    kind=ErrorKind.TOOL_CALL_FAILURE,
                    tools=tools,
                    structured=structured,
                    vision=vision,
                )
                self._provider.cooldown_model(selection.info.id, 20.0)
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
            stream_result = getattr(result, "result", None)

            def _emit_call_metrics(
                metrics: CallMetrics,
                content: CallContent,
                *,
                model_id: str,
                role: str,
                provider_id: str,
            ) -> None:
                timing = last_call_timing()
                if timing is not None and timing.ttft_ms is not None:
                    metrics.ttft_ms = timing.ttft_ms
                logger.info(
                    "router %s model %s [%s] succeeded in %.2fs %s",
                    role,
                    model_id,
                    provider_id,
                    metrics.duration_ms / 1000.0,
                    format_call_metrics(metrics),
                )
                _report_call(
                    self._sink,
                    role=role,
                    model_id=model_id,
                    provider=provider_id,
                    metrics=metrics,
                    content=content,
                    prompt_preview=prompt_preview,
                    ledger=self._ledger,
                    input_tokens_estimate=input_tokens_estimate,
                )

            if is_async_iterable(stream_result):
                result.result = MetricStream(  # type: ignore[assignment]
                    result.result,
                    started=start,
                    on_done=lambda metrics, content: _emit_call_metrics(
                        metrics,
                        content,
                        model_id=selection.info.id,
                        role=self._role,
                        provider_id=provider,
                    ),
                )
            else:
                usage = extract_usage_dict(result)
                tokens = extract_usage_tokens(result)
                metrics = CallMetrics(
                    input_tokens=tokens[0] if tokens is not None else None,
                    output_tokens=tokens[1] if tokens is not None else None,
                    cache_read_tokens=extract_cache_read(usage),
                    duration_ms=int(latency * 1000),
                    cost_usd=extract_cost(result),
                )
                _emit_call_metrics(
                    metrics,
                    content_from_messages(stream_result),
                    model_id=selection.info.id,
                    role=self._role,
                    provider_id=provider,
                )
            return result

    async def _emit(self, event: str, model_id: str, data: dict[str, Any]) -> None:
        await _emit_router_event(self._sink, self._role, event, model_id, data)

    def _emit_from_sync(self, event: str, model_id: str, data: dict[str, Any]) -> None:
        _emit_router_event_sync(self._sink, self._role, event, model_id, data)


async def _emit_router_event(
    sink: FrameworkEventSink | None,
    role: str,
    event: str,
    model_id: str,
    data: dict[str, Any],
) -> None:
    if sink is None:
        return
    await sink.accept(
        FrameworkTraceEvent(
            event=event,
            name=role,
            data={"model_id": model_id, **data},
            raw={},
        )
    )


def _emit_router_event_sync(
    sink: FrameworkEventSink | None,
    role: str,
    event: str,
    model_id: str,
    data: dict[str, Any],
) -> None:
    if sink is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_emit_router_event(sink, role, event, model_id, {"role": role, **data}))


def _model_call_timeout_seconds(provider: ModelProvider) -> float | None:
    from z_apply_core.agents.model_provider import NIMProvider

    if isinstance(provider, NIMProvider):
        value = getattr(getattr(provider._router, "config", None), "timeout_seconds", None)
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


class StaticModelRouter(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Single-model stand-in used when the provider is not NIM.

    Non-NIM providers expose exactly one model, so per-call lease rotation,
    cooldowns, failure bookkeeping, and timing measurement are meaningless.
    This middleware keeps the shared surface (one model announcement, the run
    model id, no-progress logging) without any routing behavior.
    """

    def __init__(
        self,
        provider: ModelProvider,
        role: str,
        *,
        selection: ModelSelection | None = None,
        sink: FrameworkEventSink | None = None,
        ledger: RunCallLedger | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._role = role
        self._selection = selection
        self._sink = sink
        self._ledger = ledger
        self._policy = ROLE_POLICY.get(role, {"priority": "balanced", "reasoning": True})
        self._announced = False
        class_name = type(provider).__name__
        if class_name.endswith("Provider"):
            class_name = class_name[: -len("Provider")]
        self._provider_name = class_name.lower() or type(provider).__name__

    @property
    def name(self) -> str:
        return f"StaticModelRouter[{self._role}]"

    @property
    def last_model_id(self) -> str:
        selection = self._selection
        return selection.info.id if selection is not None else ""

    def reject_active_response(self, error: ToolProtocolViolation) -> None:
        """Log only: a single-model provider has no alternative model to rotate to."""
        logger.warning(
            "router %s rejected no-progress response from %s; single-model "
            "provider, the run recovery loop owns the failure",
            self._role,
            self.last_model_id or "model",
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> Any:
        selection = self._selection
        if selection is None:
            selection = await self._provider.lease(
                tools=bool(request.tools),
                structured=request.response_format is not None,
                vision=_detect_vision(request.messages) or bool(self._policy.get("force_vision")),
                reasoning=bool(self._policy.get("reasoning", False)),
                priority=cast(
                    "Literal['fast', 'quality', 'balanced']",
                    self._policy.get("priority", "balanced"),
                ),
            )
            self._selection = selection
        if not self._announced:
            await self._emit(
                "model_selected",
                selection.info.id,
                {
                    "role": self._role,
                    "priority": self._policy.get("priority", "balanced"),
                    "tools": bool(request.tools),
                    "structured": request.response_format is not None,
                    "vision": _detect_vision(request.messages)
                    or bool(self._policy.get("force_vision")),
                    "reasoning": bool(self._policy.get("reasoning", False)),
                },
            )
            self._announced = True

        prompt_preview = _prompt_preview(request.messages)
        # OpenCode Go prefix-cache breakpoints are stamped at the wire in the
        # provider payload builder (ZenGatewayDeepSeek._get_request_payload),
        # where assistant/tool content blocks survive serialization. Stamping
        # framework messages here cannot reach those roles, and rewriting the
        # request would risk the byte-stable prefix the gateway cache needs.
        call_request = request
        input_tokens_estimate = estimate_messages_tokens(call_request.messages)
        await self._emit(
            "model_call_start",
            selection.info.id,
            {
                "role": self._role,
                "provider": self._provider_name,
                "input_tokens_estimate": input_tokens_estimate,
                "tool_count": len(request.tools or []),
                "prompt_preview": prompt_preview,
            },
        )

        start = time.monotonic()
        begin_model_call()
        try:
            result: ModelResponse[ResponseT] = await handler(call_request)
        except Exception as exc:  # noqa: BLE001 - report every failed attempt
            await self._emit(
                "model_failed",
                selection.info.id,
                {
                    "role": self._role,
                    "error_type": type(exc).__name__,
                    "error": str(exc) or "model call ended without details",
                },
            )
            logger.warning(
                "router %s model %s [%s] failed in %.2fs: %s: %s",
                self._role,
                selection.info.id,
                self._provider_name,
                time.monotonic() - start,
                type(exc).__name__,
                exc,
            )
            raise
        latency = time.monotonic() - start

        def _emit_call_metrics(
            metrics: CallMetrics,
            content: CallContent,
            *,
            model_id: str,
            role: str,
            provider_id: str,
        ) -> None:
            timing = last_call_timing()
            if timing is not None and timing.ttft_ms is not None:
                metrics.ttft_ms = timing.ttft_ms
            _report_call(
                self._sink,
                role=role,
                model_id=model_id,
                provider=provider_id,
                metrics=metrics,
                content=content,
                prompt_preview=prompt_preview,
                ledger=self._ledger,
                input_tokens_estimate=input_tokens_estimate,
            )

        stream_result = getattr(result, "result", None)
        if is_async_iterable(stream_result):
            result.result = MetricStream(  # type: ignore[assignment]
                result.result,
                started=start,
                on_done=lambda metrics, content: _emit_call_metrics(
                    metrics,
                    content,
                    model_id=selection.info.id,
                    role=self._role,
                    provider_id=self._provider_name,
                ),
            )
        else:
            usage = extract_usage_dict(result)
            tokens = extract_usage_tokens(result)
            metrics = CallMetrics(
                input_tokens=tokens[0] if tokens is not None else None,
                output_tokens=tokens[1] if tokens is not None else None,
                cache_read_tokens=extract_cache_read(usage),
                duration_ms=int(latency * 1000),
                cost_usd=extract_cost(result),
            )
            _emit_call_metrics(
                metrics,
                content_from_messages(stream_result),
                model_id=selection.info.id,
                role=self._role,
                provider_id=self._provider_name,
            )
        return result

    async def _emit(self, event: str, model_id: str, data: dict[str, Any]) -> None:
        await _emit_router_event(self._sink, self._role, event, model_id, data)


ModelRouter = NimRouterMiddleware | StaticModelRouter
"""Routing middleware surface: NIM gets lease rotation, everything else a static stand-in."""


def is_nim_provider(provider: ModelProvider) -> bool:
    """True only for the NIM provider, the sole provider with a routing catalog."""
    from z_apply_core.agents.model_provider import NIMProvider

    return isinstance(provider, NIMProvider)


def build_router_middleware(
    provider: ModelProvider,
    role: str,
    *,
    selection: ModelSelection | None = None,
    sink: FrameworkEventSink | None = None,
    ledger: RunCallLedger | None = None,
) -> ModelRouter:
    """Return the routing middleware for NIM, a static single-model stand-in otherwise."""
    if is_nim_provider(provider):
        return NimRouterMiddleware(
            provider,
            role=role,
            initial_selection=selection,
            sink=sink,
            ledger=ledger,
        )
    return StaticModelRouter(
        provider,
        role=role,
        selection=selection,
        sink=sink,
        ledger=ledger,
    )
