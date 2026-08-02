from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import BaseMessage

from z_apply_core.context.context_budget import estimate_tokens
from z_apply_core.context.run_context import RunContext
from z_apply_core.stream_events import TokenUsageEvent

logger = logging.getLogger(__name__)


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    """Estimate prompt tokens for messages with str or list-of-part content.

    Unlike ``context_budget.estimate_messages_tokens`` (a raw character count
    used for budget decisions), this applies the shared ``estimate_tokens``
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

    def __str__(self) -> str:
        return (
            f"prompt_tokens={self.prompt_tokens} "
            f"tool_schema_tokens={self.tool_schema_tokens} "
            f"messages={self.message_count} "
            f"tools={self.tool_count}"
        )


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
    ) -> None:
        super().__init__()
        self._run_context = run_context
        self._emit = emit
        self._last_before_usage: TokenUsage | None = None
        self._last_after_usage: TokenUsage | None = None

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
        result = await handler(request)
        after = _safe_usage(lambda: _usage_from_response(result, request))
        if after is not None:
            self._last_after_usage = after
            self._note(after)
            self._publish(after, request)
        return result

    def _note(self, usage: TokenUsage) -> None:
        if self._run_context is None:
            return
        try:
            self._run_context.note_usage(usage)
        except Exception as exc:
            logger.warning("token metric could not record usage: %s", exc)

    def _publish(self, usage: TokenUsage, request: ModelRequest[ContextT]) -> None:
        if self._emit is None:
            return
        try:
            self._emit(
                TokenUsageEvent(
                    run_id="",
                    usage=usage,
                    model=_model_label(request),
                    provider=_provider_label(request),
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
) -> TokenUsage:
    response_messages = list(response.result or [])
    messages = [*request.messages, *response_messages]
    return TokenUsage(
        prompt_tokens=estimate_messages_tokens(messages),
        tool_schema_tokens=estimate_schema_tokens(request.tools),
        message_count=len(messages),
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
