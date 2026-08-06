from __future__ import annotations

import contextvars
import inspect
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CallMetrics:
    """Measured usage and timing for one model call."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int = 0
    ttft_ms: int | None = None

    @property
    def tok_per_second(self) -> float | None:
        if not self.output_tokens:
            return None
        if self.ttft_ms is not None and 0 < self.ttft_ms < self.duration_ms:
            generation_ms = self.duration_ms - self.ttft_ms
        else:
            generation_ms = self.duration_ms
        if generation_ms <= 0:
            return None
        return self.output_tokens / (generation_ms / 1000.0)


def is_async_iterable(value: Any) -> bool:
    if inspect.isasyncgen(value):
        return True
    aiter = getattr(value, "__aiter__", None)
    if aiter is None:
        return False
    try:
        return inspect.isawaitable(aiter())
    except TypeError:
        return False


def extract_usage_tokens(
    result: Any, stream_usage: dict[str, Any] | None = None
) -> tuple[int, int] | None:
    """Extract (input, output) token counts from a model response.

    Accepts ``usage_metadata`` and ``response_metadata["usage"]`` on the result
    or its messages, covering both ChatOpenAI keys (``prompt_tokens`` /
    ``completion_tokens``) and ChatNVIDIA keys (``input_tokens`` /
    ``output_tokens``). Chunk-level usage collected from a stream takes
    priority when passed as ``stream_usage``.
    """
    candidates: list[Any] = []
    if stream_usage is not None:
        candidates.append(stream_usage)
    if result is not None:
        candidates.append(getattr(result, "usage_metadata", None) or {})
        candidates.append((getattr(result, "response_metadata", None) or {}).get("usage") or {})
        payload = result
        if not isinstance(payload, (list, tuple)):
            inner = getattr(payload, "result", None)
            if isinstance(inner, (list, tuple)):
                payload = inner
        messages = getattr(payload, "messages", None)
        if isinstance(payload, (list, tuple)):
            messages = payload
        for message in messages or []:
            candidates.append(getattr(message, "usage_metadata", None) or {})
            candidates.append(
                (getattr(message, "response_metadata", None) or {}).get("usage") or {}
            )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        usage = _usage_tokens(candidate)
        if usage is not None:
            return usage
    return None


def chunk_usage(item: Any) -> dict[str, Any] | None:
    """Return token usage carried by one streamed chunk, if any."""
    usage = getattr(item, "usage_metadata", None)
    if usage is None:
        usage = (getattr(item, "response_metadata", None) or {}).get("usage")
    if not isinstance(usage, dict):
        return None
    if _usage_tokens(usage) is None:
        return None
    return usage


class MetricStream:
    """Wrap a model stream to measure TTFT and collect final token usage.

    Iteration is delegated untouched; on exhaustion (or close) the wrapper
    reports a ``CallMetrics`` snapshot to ``on_done``.
    """

    def __init__(
        self,
        stream: Any,
        *,
        started: float,
        on_done: Callable[[CallMetrics], None] | None = None,
    ) -> None:
        self._stream = stream
        self._started = started
        self._on_done = on_done
        self._first_at: float | None = None
        self._usage: dict[str, Any] | None = None

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        try:
            async for item in self._stream:
                if self._first_at is None:
                    self._first_at = time.monotonic()
                usage = chunk_usage(item)
                if usage is not None:
                    self._usage = usage
                yield item
        finally:
            if self._on_done is not None:
                try:
                    self._on_done(self._snapshot())
                except Exception:
                    logger.exception("metric stream failed to report call metrics")

    def _snapshot(self) -> CallMetrics:
        now = time.monotonic()
        duration_ms = int((now - self._started) * 1000)
        ttft_ms = (
            int((self._first_at - self._started) * 1000)
            if self._first_at is not None
            else duration_ms
        )
        tokens = extract_usage_tokens(None, self._usage) if self._usage is not None else None
        return CallMetrics(
            input_tokens=tokens[0] if tokens is not None else None,
            output_tokens=tokens[1] if tokens is not None else None,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
        )


def format_call_metrics(metrics: CallMetrics) -> str:
    """Render one call's metrics as ``in=.. out=.. ttft=.. tok/s=..``."""
    ttft = "n/a" if metrics.ttft_ms is None else f"{metrics.ttft_ms:.0f}ms"
    tok_s = "n/a" if metrics.tok_per_second is None else f"{metrics.tok_per_second:.1f}"
    return (
        f"in={_int_or_na(metrics.input_tokens)} "
        f"out={_int_or_na(metrics.output_tokens)} "
        f"ttft={ttft} tok/s={tok_s}"
    )


def _usage_tokens(candidate: dict[str, Any]) -> tuple[int, int] | None:
    prompt = candidate.get("prompt_tokens", candidate.get("input_tokens"))
    completion = candidate.get("completion_tokens", candidate.get("output_tokens"))
    if isinstance(prompt, int) and isinstance(completion, int):
        return prompt, completion
    return None


def _int_or_na(value: int | None) -> str:
    return "n/a" if value is None else str(value)


@dataclass(slots=True)
class CallTiming:
    """Request-start and first-chunk timestamps for one model call."""

    started: float
    first_chunk_at: float | None = None

    @property
    def ttft_ms(self) -> int | None:
        if self.first_chunk_at is None:
            return None
        return int((self.first_chunk_at - self.started) * 1000)


_MODEL_CALL_TIMING: contextvars.ContextVar[CallTiming | None] = contextvars.ContextVar(
    "z_apply_model_call_timing", default=None
)


def begin_model_call() -> CallTiming:
    """Mark the start of one model call in the current task context."""
    timing = CallTiming(started=time.monotonic())
    _MODEL_CALL_TIMING.set(timing)
    return timing


def mark_first_chunk() -> None:
    """Record the first streamed chunk of the current model call, if any."""
    timing = _MODEL_CALL_TIMING.get()
    if timing is not None and timing.first_chunk_at is None:
        timing.first_chunk_at = time.monotonic()


def last_call_timing() -> CallTiming | None:
    """Return the most recent model call timing in the current task context."""
    return _MODEL_CALL_TIMING.get()


def attach_first_token_callback(model: Any) -> BaseCallbackHandler:
    """Attach a token callback that records first-chunk timing per model call.

    The middleware that owns a model call calls :func:`begin_model_call` before
    invoking the handler and reads :func:`last_call_timing` when the call
    completes. Token callbacks fire inside the same task context during stream
    consumption, so the recorded first-chunk time is the honest model-side
    TTFT. Attaching is idempotent and pydantic-safe (``callbacks`` is a model
    field, unlike ``astream``).
    """
    handler = _FirstTokenCallback()
    configured = model.callbacks
    callbacks: list[Any]
    if isinstance(configured, list):
        callbacks = list(configured)
    else:
        callbacks = list(getattr(configured, "handlers", ()))
    if not any(getattr(cb, "name", None) == _FirstTokenCallback.NAME for cb in callbacks):
        callbacks.append(handler)
        model.callbacks = callbacks
    return handler


class _FirstTokenCallback(BaseCallbackHandler):
    """Marks the first model token of the current call into the task context."""

    NAME = "z_apply_first_token_timing"

    def __init__(self) -> None:
        super().__init__()
        self.name = self.NAME

    def on_llm_new_token(self, *args: Any, **kwargs: Any) -> None:
        mark_first_chunk()

    def on_chat_model_stream(self, *args: Any, **kwargs: Any) -> None:
        mark_first_chunk()
