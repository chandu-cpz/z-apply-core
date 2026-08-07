from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import BaseMessage

from z_apply_core.context.context_budget import estimate_tokens
from z_apply_core.context.model_metrics import (
    chunk_usage,
    extract_usage_tokens,
    is_async_iterable,
)
from z_apply_core.context.run_context import RunContext
from z_apply_core.stream_events import TokenUsageEvent

logger = logging.getLogger(__name__)


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    """Estimate prompt tokens for messages with str or list-of-part content.

    Unlike a raw character count, this applies the shared ``estimate_tokens``
    heuristic to every text part so structured content is counted as well.
    """
    total = 0
    for message in messages:
        content = getattr(message, "content", None)
        if content is None:
            continue
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            total += sum(_content_part_tokens(part) for part in content)
        else:
            total += estimate_tokens(str(content))
    return total


def _content_part_tokens(part: Any) -> int:
    if isinstance(part, dict):
        text = part.get("text")
        if isinstance(text, str):
            return estimate_tokens(text)
        return estimate_tokens(str(part))
    return estimate_tokens(str(part))


def estimate_schema_tokens(tools: list[Any]) -> int:
    """Deterministic estimate of the token cost of the model-visible schemas."""
    total = 0
    for tool in tools:
        try:
            if isinstance(tool, dict):
                total += _dict_tool_tokens(tool)
            elif tool is not None:
                total += _object_tool_tokens(tool)
        except Exception:
            logger.warning("token metric skipped unreadable tool schema: %r", tool)
    return total


def _dict_tool_tokens(tool: dict[str, Any]) -> int:
    function = tool.get("function")
    source = function if isinstance(function, dict) else tool
    name = _string_value(source, "name")
    description = _string_value(source, "description")
    schema = source.get("parameters")
    if schema is None:
        schema = source.get("args_schema")
    if schema is None:
        schema = source.get("input_schema")
    return (
        estimate_tokens(name)
        + estimate_tokens(description)
        + estimate_tokens(_serialize_schema(schema))
    )


def _object_tool_tokens(tool: Any) -> int:
    name = str(getattr(tool, "name", "") or "")
    description = str(getattr(tool, "description", "") or "")
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        schema = getattr(tool, "parameters", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    return (
        estimate_tokens(name)
        + estimate_tokens(description)
        + estimate_tokens(_serialize_schema(schema))
    )


def _string_value(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _serialize_schema(schema: Any) -> str:
    if schema is None:
        return ""
    if isinstance(schema, dict):
        return str(schema)
    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        try:
            return str(model_json_schema())
        except Exception:
            return str(schema)
    return str(schema)


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int
    tool_schema_tokens: int
    message_count: int
    tool_count: int
    completion_tokens: int = 0

    def __str__(self) -> str:
        return (
            f"prompt_tokens={self.prompt_tokens} "
            f"completion_tokens={self.completion_tokens} "
            f"tool_schema_tokens={self.tool_schema_tokens} "
            f"messages={self.message_count} "
            f"tools={self.tool_count}"
        )


class _StreamResult:
    def __init__(self, result: Any) -> None:
        self.result = result


class TokenMetricMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Measure model-visible prompt and schema token usage around each model call.

    Computes usage from the request before the call and from the combined
    request + response after the call. Measurement failures are logged and
    swallowed so they can never break the real model call.
    """

    def __init__(
        self,
        *,
        run_context: RunContext | None = None,
        emit: Callable[[object], None] | None = None,
        agent: str | None = None,
    ) -> None:
        super().__init__()
        self._run_context = run_context
        self._emit = emit
        self._agent = agent
        self._last_before_usage: TokenUsage | None = None
        self._last_after_usage: TokenUsage | None = None
        self._last_ttft_ms: int | None = None
        self._last_stream_usage: dict[str, Any] | None = None

    @property
    def last_before_usage(self) -> TokenUsage | None:
        return self._last_before_usage

    @property
    def last_after_usage(self) -> TokenUsage | None:
        return self._last_after_usage

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        before = _safe_usage(lambda: _usage_from_request(request))
        if before is not None:
            self._last_before_usage = before
            self._note(before)
        t0 = time.monotonic()
        result = await handler(request)
        duration_ms = int((time.monotonic() - t0) * 1000)
        if is_async_iterable(result.result):
            self._last_ttft_ms = 0
            result.result = self._timed_stream(  # type: ignore[assignment]
                result.result, t0, request, before, duration_ms
            )
        else:
            self._last_ttft_ms = duration_ms
            self._finish_call(request, before, duration_ms, result)
        return result

    def _finish_call(
        self,
        request: ModelRequest[ContextT],
        before: TokenUsage | None,
        duration_ms: int,
        response: Any,
    ) -> None:
        after = _safe_usage(
            lambda: _usage_from_response(response, request, self._last_stream_usage)
        )
        if after is None:
            return
        self._last_after_usage = after
        self._note(after)
        self._note_totals(after)
        output_tokens_estimate = (
            after.completion_tokens
            if after.completion_tokens > 0
            else (max(0, after.prompt_tokens - before.prompt_tokens) if before is not None else 0)
        )
        ttft_ms = self._last_ttft_ms
        if ttft_ms is not None and 0 < ttft_ms < duration_ms:
            generation_ms = duration_ms - ttft_ms
        else:
            generation_ms = duration_ms
        tok_per_second = (
            output_tokens_estimate / (generation_ms / 1000.0)
            if generation_ms > 0
            else 0.0
        )
        self._publish(after, request, duration_ms, output_tokens_estimate, tok_per_second)

    def _measure_ttft(
        self,
        result: ModelResponse[ResponseT],
        t0: float,
        duration_ms: int,
    ) -> None:
        response = result.result
        if is_async_iterable(response):
            self._last_ttft_ms = 0
        else:
            self._last_ttft_ms = duration_ms

    async def _timed_stream(
        self,
        stream: Any,
        t0: float,
        request: ModelRequest[ContextT],
        before: TokenUsage | None,
        duration_ms: int,
    ) -> AsyncIterator[Any]:
        first: float | None = None
        try:
            async for item in stream:
                if first is None:
                    first = time.monotonic()
                usage = chunk_usage(item)
                if usage is not None:
                    self._last_stream_usage = usage
                yield item
        finally:
            if first is not None:
                self._last_ttft_ms = int((first - t0) * 1000)
            else:
                self._last_ttft_ms = int((time.monotonic() - t0) * 1000)
            self._finish_call(request, before, duration_ms, _StreamResult(stream))

    def _note(self, usage: TokenUsage) -> None:
        if self._run_context is None:
            return
        try:
            self._run_context.note_usage(usage)
        except Exception as exc:
            logger.warning("token metric could not record usage: %s", exc)

    def _note_totals(self, usage: TokenUsage) -> None:
        if self._run_context is None:
            return
        try:
            previous = self._run_context.usage_totals
            self._run_context.usage_totals = TokenUsage(
                prompt_tokens=(previous.prompt_tokens if previous else 0) + usage.prompt_tokens,
                completion_tokens=(previous.completion_tokens if previous else 0)
                + usage.completion_tokens,
                tool_schema_tokens=(previous.tool_schema_tokens if previous else 0)
                + usage.tool_schema_tokens,
                message_count=(previous.message_count if previous else 0) + usage.message_count,
                tool_count=(previous.tool_count if previous else 0) + usage.tool_count,
            )
        except Exception as exc:
            logger.warning("token metric could not record usage totals: %s", exc)

    def _publish(
        self,
        usage: TokenUsage,
        request: ModelRequest[ContextT],
        duration_ms: int,
        output_tokens_estimate: int,
        tok_per_second: float,
    ) -> None:
        if self._emit is None:
            return
        try:
            self._emit(
                TokenUsageEvent(
                    run_id="",
                    usage=usage,
                    model=_model_label(request),
                    provider=_provider_label(request),
                    agent=self._agent,
                    duration_ms=duration_ms,
                    ttft_ms=0 if self._last_ttft_ms is None else self._last_ttft_ms,
                    output_tokens_estimate=output_tokens_estimate,
                    tok_per_second=tok_per_second,
                )
            )
        except Exception as exc:
            logger.warning("token metric could not emit usage event: %s", exc)


def _usage_from_request(request: ModelRequest[ContextT]) -> TokenUsage:
    return TokenUsage(
        prompt_tokens=estimate_messages_tokens(request.messages),
        tool_schema_tokens=estimate_schema_tokens(request.tools),
        message_count=len(request.messages),
        tool_count=len(request.tools),
    )


def _usage_from_response(
    response: ModelResponse[ResponseT],
    request: ModelRequest[ContextT],
    stream_usage: dict[str, Any] | None = None,
) -> TokenUsage:
    result = response.result
    real = extract_usage_tokens(result, stream_usage)
    if is_async_iterable(result):
        response_messages: list[Any] = []
    else:
        response_messages = list(result or [])
    messages = [*request.messages, *response_messages]
    message_count = len(messages) + (1 if is_async_iterable(result) else 0)
    if real is not None:
        return TokenUsage(
            prompt_tokens=real[0],
            completion_tokens=real[1],
            tool_schema_tokens=estimate_schema_tokens(request.tools),
            message_count=message_count,
            tool_count=len(request.tools),
        )
    return TokenUsage(
        prompt_tokens=estimate_messages_tokens(messages),
        tool_schema_tokens=estimate_schema_tokens(request.tools),
        message_count=message_count,
        tool_count=len(request.tools),
    )


def _safe_usage(compute: Callable[[], TokenUsage]) -> TokenUsage | None:
    try:
        return compute()
    except Exception as exc:
        logger.warning("token metric estimate failed: %s", exc)
        return None


def _model_label(request: ModelRequest[ContextT]) -> str | None:
    model = getattr(request, "model", None)
    if model is None:
        return None
    return str(
        getattr(model, "model_name", None)
        or getattr(model, "name", None)
        or type(model).__name__
    )


def _provider_label(request: ModelRequest[ContextT]) -> str | None:
    model = getattr(request, "model", None)
    if model is None:
        return None
    module = type(model).__module__ or ""
    return module.split(".")[0] if module else None
