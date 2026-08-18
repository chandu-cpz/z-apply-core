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
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
)

from z_apply_core.agents.model_provider import ModelProvider, ModelSelection
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
    is_async_iterable,
    last_call_timing,
)
from z_apply_core.context.token_metric import estimate_messages_tokens
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

logger = logging.getLogger(__name__)

# Role → routing policy.
# ``reasoning`` / ``priority`` are role-based; ``force_vision`` always treats
# the request as needing a vision-capable model.
ROLE_POLICY: dict[str, dict[str, Any]] = {
    "orchestrator": {
        "priority": "balanced",
        "reasoning": True,
    },
    "authenticate_default_account": {
        "priority": "balanced",
        "reasoning": True,
        "reasoning_effort": "low",
    },
    "AuthenticationSpecialist": {
        "priority": "balanced",
        "reasoning": True,
        "reasoning_effort": "low",
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
    calls so the Rich stream shows every LLM call in every phase. Both carry
    the resolved cost (gateway-reported or rate-card estimate) and tokens/s so
    the backend can persist an auditable per-call ledger without re-deriving
    rates.
    """
    cost_usd: float | None = metrics.cost_usd
    if ledger is not None:
        # Record first so the ledger's rate-card fallback resolves the cost
        # when the gateway reports none; that resolved value is what ships.
        input_tokens = (
            metrics.input_tokens
            if metrics.input_tokens is not None
            else (input_tokens_estimate if input_tokens_estimate is not None else 0)
        )
        output_tokens = metrics.output_tokens if metrics.output_tokens is not None else 0
        entry = ledger.record(
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
        cost_usd = entry.cost.usd
    common = {
        "role": role,
        "provider": provider,
        "duration_ms": metrics.duration_ms,
        "ttft_ms": metrics.ttft_ms,
        "input_tokens": metrics.input_tokens,
        "output_tokens": metrics.output_tokens,
        "cache_read_tokens": metrics.cache_read_tokens,
        "tok_per_second": metrics.tok_per_second,
        "cost_usd": round(cost_usd, 6) if cost_usd is not None else None,
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


def _emit_live_progress(
    sink: FrameworkEventSink | None,
    role: str,
    model_id: str,
    provider_id: str,
    metrics: CallMetrics,
    content: CallContent,
) -> None:
    """Publish rolling generation metrics to the live stream (~every 2s).

    The provider reports final usage only on the last chunk, so the token count
    is a character-based estimate from the captured content; TTFT and tok/s are
    rolling and correct the moment the first chunk lands.
    """
    text = content.text or ""
    reasoning = content.reasoning or ""
    output_estimate = (len(text) + len(reasoning)) // 4
    ttft_ms = metrics.ttft_ms
    generation_ms = metrics.duration_ms - (ttft_ms or 0)
    tok_per_second = (output_estimate / (generation_ms / 1000.0)) if generation_ms > 0 else 0.0
    _emit_router_event_sync(
        sink,
        role,
        "model_call_progress",
        model_id,
        {
            "provider": provider_id,
            "model": model_id,
            "ttft_ms": ttft_ms,
            "tok_per_second": round(tok_per_second, 1),
            "output_tokens_estimate": output_estimate,
            "duration_ms": metrics.duration_ms,
        },
    )


class ModelRouter(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Middleware for model invocation lifecycle, telemetry, metrics, and event streaming."""

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
        return f"ModelRouter[{self._role}]"

    @property
    def last_model_id(self) -> str:
        selection = self._selection
        return selection.info.id if selection is not None else ""

    def reject_active_response(self, error: ToolProtocolViolation) -> None:
        """Log only: single-model provider has no alternative model to rotate to."""
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
                reasoning_effort=self._policy.get("reasoning_effort"),
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
                on_progress=lambda metrics, content: _emit_live_progress(
                    self._sink,
                    self._role,
                    selection.info.id,
                    self._provider_name,
                    metrics,
                    content,
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

    def _emit_from_sync(self, event: str, model_id: str, data: dict[str, Any]) -> None:
        _emit_router_event_sync(self._sink, self._role, event, model_id, data)


StaticModelRouter = ModelRouter


def build_router_middleware(
    provider: ModelProvider,
    role: str,
    *,
    selection: ModelSelection | None = None,
    sink: FrameworkEventSink | None = None,
    ledger: RunCallLedger | None = None,
) -> ModelRouter:
    """Return the model middleware for telemetry and invocation handling."""
    return ModelRouter(
        provider,
        role=role,
        selection=selection,
        sink=sink,
        ledger=ledger,
    )
