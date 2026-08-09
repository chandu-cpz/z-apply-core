from __future__ import annotations

import contextvars
import inspect
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CallMetrics:
    """Measured usage and timing for one model call."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    duration_ms: int = 0
    ttft_ms: int | None = None
    cost_usd: float | None = None

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


@dataclass(slots=True)
class CallContent:
    """Model-visible content produced by one model call.

    Captured from the streamed chunks (streaming) or the final response
    messages (non-streaming) so the terminal can show exactly what the model
    produced even when the graph's stream projections are silent.
    """

    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def capture_chunk_content(item: Any, content: CallContent) -> None:
    """Accumulate text, reasoning, and tool-call chunks from one streamed item.

    Handles the shapes used by OpenAI-compatible providers (including the
    langchain-deepseek / Agnes reasoning carrier ``reasoning_content``) and
    Anthropic-style content blocks (``text`` / ``thinking`` / ``reasoning``).
    Tool calls are accumulated per index so multi-call responses render fully.
    """
    raw = getattr(item, "content", None)
    if isinstance(raw, str):
        if raw:
            content.text += raw
    elif isinstance(raw, list):
        for block in raw:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))
            if block_type in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    content.text += text
            elif block_type in {"thinking", "reasoning", "redacted_thinking"}:
                reasoning = block.get("reasoning_content") or block.get("text")
                if isinstance(reasoning, str):
                    content.reasoning += reasoning
    reasoning = getattr(item, "reasoning_content", None)
    if not isinstance(reasoning, str):
        additional = getattr(item, "additional_kwargs", None) or {}
        if isinstance(additional, dict):
            reasoning = additional.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        content.reasoning += reasoning
    _capture_tool_call_chunks(item, content)


def _capture_tool_call_chunks(item: Any, content: CallContent) -> None:
    chunks = getattr(item, "tool_call_chunks", None)
    if not chunks:
        return
    by_index: dict[int, dict[str, Any]] = {}
    for entry in content.tool_calls:
        index = entry.get("index")
        if isinstance(index, int):
            by_index[index] = entry
    for chunk in chunks:
        index = getattr(chunk, "index", None)
        if not isinstance(index, int):
            continue
        existing = by_index.get(index)
        if existing is None:
            existing = {"index": index, "name": "", "id": "", "args": ""}
            by_index[index] = existing
            content.tool_calls.append(existing)
        name = getattr(chunk, "name", None)
        if isinstance(name, str) and name:
            existing["name"] = name
        call_id = getattr(chunk, "id", None)
        if isinstance(call_id, str) and call_id:
            existing["id"] = call_id
        args = getattr(chunk, "args", None)
        if isinstance(args, str):
            existing["args"] += args


def content_from_messages(messages: Any) -> CallContent:
    """Extract text, reasoning, and tool calls from a final non-streamed result.

    ``messages`` is the ``ModelResponse.result`` sequence of chat messages.
    """
    content = CallContent()
    if not isinstance(messages, (list, tuple)):
        return content
    for message in messages:
        if isinstance(message, ToolMessage):
            # Tool output is not model-generated content; it is rendered by
            # the tool panels and must not leak into the response text.
            continue
        capture_chunk_content(message, content)
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                content.tool_calls.append(
                    {
                        "index": len(content.tool_calls),
                        "name": str(call.get("name", "")),
                        "id": str(call.get("id", "")),
                        "args": _compact_json_args(call.get("args")),
                    }
                )
    return content


def _compact_json_args(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    try:
        import json

        return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(args)


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
    for candidate in _usage_candidates(result, stream_usage):
        usage = _usage_tokens(candidate)
        if usage is not None:
            return usage
    return None


def extract_usage_dict(
    result: Any, stream_usage: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return the first valid usage dict from a model response, if any.

    Shares the candidate walk with :func:`extract_usage_tokens` so callers can
    read provider-specific usage fields (for example prompt-cache counters)
    from the same payload the token counts came from.
    """
    for candidate in _usage_candidates(result, stream_usage):
        if _usage_tokens(candidate) is not None:
            return candidate
    return None


def extract_cache_read(usage: dict[str, Any] | None) -> int | None:
    """Extract prompt-cache read tokens from a usage dict, if reported.

    Reads the OpenAI-style ``prompt_tokens_details.cached_tokens`` (which
    langchain surfaces as ``input_token_details.cache_read``) and the
    DeepSeek-style ``prompt_cache_hit_tokens``. Both appear in OpenCode Go
    chat-completions usage responses.
    """
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_token_details")
    if isinstance(details, dict):
        cached = details.get("cache_read")
        if isinstance(cached, int):
            return cached
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, int):
            return cached
    cached = usage.get("prompt_cache_hit_tokens")
    if isinstance(cached, int):
        return cached
    return None


def _usage_candidates(result: Any, stream_usage: dict[str, Any] | None) -> list[dict[str, Any]]:
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
    return candidates


def extract_cost(result: Any) -> float | None:
    """Extract a gateway-reported dollar cost from a model response, if any.

    The OpenCode gateway returns a top-level ``cost`` field (string dollars,
    ``"0"`` for Go subscription requests). It may surface on the raw response
    object, in ``model_extra``, or in a message's ``additional_kwargs``;
    ``"0"``/``0`` means no per-request charge was reported and returns None.
    """
    candidates: list[Any] = list(_usage_candidates(result, None))
    payload = result
    if not isinstance(payload, (list, tuple)):
        inner = getattr(payload, "result", None)
        if isinstance(inner, (list, tuple)):
            payload = inner
    candidates.extend(
        getattr(payload, "messages", None)
        or (payload if isinstance(payload, (list, tuple)) else ())
    )
    for candidate in candidates:
        raw = getattr(candidate, "cost", None)
        if raw is None:
            model_extra = getattr(candidate, "model_extra", None) or {}
            raw = model_extra.get("cost") if isinstance(model_extra, dict) else None
        if raw is None and isinstance(candidate, dict):
            raw = candidate.get("cost")
        if raw is None:
            additional = getattr(candidate, "additional_kwargs", None)
            if isinstance(additional, dict):
                raw = additional.get("cost")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
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
    """Wrap a model stream to measure TTFT, collect token usage, and capture
    the streamed content (text, reasoning, tool calls).

    Iteration is delegated untouched; on exhaustion (or close) the wrapper
    reports a ``CallMetrics`` snapshot and the accumulated
    :class:`CallContent` to ``on_done``.
    """

    def __init__(
        self,
        stream: Any,
        *,
        started: float,
        on_done: Callable[[CallMetrics, CallContent], None] | None = None,
        on_progress: Callable[[CallMetrics, CallContent], None] | None = None,
        progress_interval: float = 2.0,
    ) -> None:
        self._stream = stream
        self._started = started
        self._on_done = on_done
        self._on_progress = on_progress
        self._progress_interval = progress_interval
        self._first_at: float | None = None
        self._usage: dict[str, Any] | None = None
        self._cost: Any = None
        self._content = CallContent()
        self._last_progress = started

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
                cost = getattr(item, "cost", None)
                if cost is None:
                    model_extra = getattr(item, "model_extra", None) or {}
                    cost = model_extra.get("cost") if isinstance(model_extra, dict) else None
                if cost is not None:
                    self._cost = cost
                capture_chunk_content(item, self._content)
                if self._on_progress is not None:
                    now = time.monotonic()
                    if now - self._last_progress >= self._progress_interval:
                        self._last_progress = now
                        try:
                            self._on_progress(self._progress_snapshot(now), self._content)
                        except Exception:
                            logger.exception("metric stream failed to report progress")
                yield item
        finally:
            if self._on_done is not None:
                try:
                    self._on_done(self._snapshot(), self._content)
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
        cost_usd: float | None = None
        if self._cost is not None:
            try:
                parsed = float(self._cost)
            except (TypeError, ValueError):
                parsed = 0.0
            if parsed > 0:
                cost_usd = parsed
        return CallMetrics(
            input_tokens=tokens[0] if tokens is not None else None,
            output_tokens=tokens[1] if tokens is not None else None,
            cache_read_tokens=extract_cache_read(self._usage),
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            cost_usd=cost_usd,
        )

    def _progress_snapshot(self, now: float) -> CallMetrics:
        """Mid-stream metrics for live progress reporting: TTFT once known,
        elapsed duration, and no final usage counts (unknown until the last
        chunk). The middleware derives a token estimate from captured content.
        """
        duration_ms = int((now - self._started) * 1000)
        ttft_ms = (
            int((self._first_at - self._started) * 1000)
            if self._first_at is not None
            else None
        )
        return CallMetrics(
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            cost_usd=None,
        )


def format_call_metrics(metrics: CallMetrics) -> str:
    """Render one call's metrics as ``in=.. out=.. [cache=..] ttft=.. tok/s=..``."""
    ttft = "n/a" if metrics.ttft_ms is None else f"{metrics.ttft_ms:.0f}ms"
    tok_s = "n/a" if metrics.tok_per_second is None else f"{metrics.tok_per_second:.1f}"
    cache = "n/a" if metrics.cache_read_tokens is None else str(metrics.cache_read_tokens)
    return (
        f"in={_int_or_na(metrics.input_tokens)} "
        f"out={_int_or_na(metrics.output_tokens)} "
        f"cache={cache} "
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
