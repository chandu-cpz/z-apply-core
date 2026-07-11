from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent, V3RunResult

logger = logging.getLogger(__name__)


async def consume_deepagent_stream(
    stream: Any,
    *,
    sink: FrameworkEventSink | None = None,
    root_source: str = "orchestrator",
) -> V3RunResult:
    started = time.monotonic()
    resolved = await _resolve_stream(stream)
    tool_trace: list[dict[str, Any]] = []

    async with _managed_stream(resolved) as active_stream:
        await asyncio.gather(
            _consume_messages(root_source, _projection(active_stream, "messages"), sink),
            _consume_tool_calls(
                root_source,
                _projection(active_stream, "tool_calls"),
                sink,
                tool_trace,
            ),
            _consume_subagents(_projection(active_stream, "subagents"), sink, tool_trace),
        )
        output = await _read_output(active_stream)
        output["_z_apply_tool_trace"] = tool_trace

    return V3RunResult(
        output=output,
        event_count=0,
        errors=[],
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def _consume_subagents(
    subagents: AsyncIterable[Any],
    sink: FrameworkEventSink | None,
    tool_trace: list[dict[str, Any]],
) -> None:
    async for subagent in subagents:
        name = str(getattr(subagent, "name", "subagent"))
        await _emit(
            sink,
            "agent_lifecycle",
            name,
            {
                "status": str(getattr(subagent, "status", "started")),
                "path": str(getattr(subagent, "path", "")),
            },
        )
        try:
            await asyncio.gather(
                _consume_messages(name, _projection(subagent, "messages"), sink),
                _consume_tool_calls(
                    name,
                    _projection(subagent, "tool_calls"),
                    sink,
                    tool_trace,
                ),
                _consume_subagents(_projection(subagent, "subagents"), sink, tool_trace),
            )
            await _read_output(subagent)
            await _emit(sink, "agent_lifecycle", name, {"status": "completed"})
        except Exception as exc:
            logger.exception("DeepAgents subagent %s failed", name)
            await _emit(sink, "agent_lifecycle", name, {"status": "failed", "error": str(exc)})


async def _consume_messages(
    source: str,
    messages: AsyncIterable[Any],
    sink: FrameworkEventSink | None,
) -> None:
    async for message_stream in messages:
        await asyncio.gather(
            _consume_message_text(source, message_stream.text, sink),
            _consume_message_reasoning(source, message_stream.reasoning, sink),
            _consume_message_tool_call_chunks(source, message_stream.tool_calls, sink),
            _read_message_output(message_stream),
        )


async def _consume_message_text(
    source: str,
    text: AsyncIterable[str],
    sink: FrameworkEventSink | None,
) -> None:
    async for delta in text:
        if not delta:
            continue
        if source in {"authenticate_default_account"}:
            continue
        await _emit(
            sink,
            "agent_message_delta",
            source,
            {
                "kind": "text",
                "delta": delta,
            },
        )


async def _consume_message_reasoning(
    source: str,
    reasoning: AsyncIterable[str],
    sink: FrameworkEventSink | None,
) -> None:
    async for delta in reasoning:
        if not delta:
            continue
        await _emit(
            sink,
            "agent_message_delta",
            source,
            {
                "kind": "reasoning",
                "delta": delta,
            },
        )


async def _consume_message_tool_call_chunks(
    source: str,
    tool_calls: AsyncIterable[Any],
    sink: FrameworkEventSink | None,
) -> None:
    async for chunk in tool_calls:
        await _emit(
            sink,
            "agent_model_tool_call",
            source,
            {
                "chunk": chunk,
            },
        )


async def _read_message_output(message_stream: Any) -> None:
    await message_stream.output


async def _consume_tool_calls(
    source: str,
    tool_calls: AsyncIterable[Any],
    sink: FrameworkEventSink | None,
    tool_trace: list[dict[str, Any]],
) -> None:
    async for call in tool_calls:
        tool_name = str(call.tool_name)
        await _emit(
            sink,
            "agent_tool_start",
            source,
            {
                "tool_name": tool_name,
                "input": call.input,
            },
        )
        async for delta in call.output_deltas:
            await _emit(
                sink,
                "agent_tool_delta",
                source,
                {
                    "tool_name": tool_name,
                    "delta": str(delta),
                },
            )

        await _emit(
            sink,
            "agent_tool_end",
            source,
            {
                "tool_name": tool_name,
                "output": call.output,
                "error": str(call.error) if call.error is not None else "",
                "completed": call.completed,
            },
        )
        tool_trace.append(
            {
                "source": source,
                "tool_name": tool_name,
                "input": call.input,
                "output": call.output,
                "error": str(call.error) if call.error is not None else "",
                "completed": bool(call.completed),
            }
        )


async def _emit(
    sink: FrameworkEventSink | None,
    event: str,
    name: str,
    data: dict[str, Any],
) -> None:
    if sink is None:
        return
    await sink.accept(
        FrameworkTraceEvent(
            event=event,
            name=name,
            data=data,
            raw={"event": event, "name": name, "data": data},
        )
    )


async def _resolve_stream(stream: Any) -> Any:
    while inspect.isawaitable(stream):
        stream = await stream
    return stream


@asynccontextmanager
async def _managed_stream(stream: Any) -> AsyncIterator[Any]:
    if hasattr(stream, "__aenter__") and hasattr(stream, "__aexit__"):
        async with stream:
            yield stream
        return
    yield stream


def _projection(source: Any, name: str) -> AsyncIterable[Any]:
    value = getattr(source, name, None)
    if value is None:
        return _empty_async_iter()
    return cast(AsyncIterable[Any], value)


async def _read_output(stream: Any) -> dict[str, Any]:
    output = getattr(stream, "output", None)
    if output is None:
        return {}
    value = output() if callable(output) else output
    value = await _maybe_await(value)
    return dict(value) if isinstance(value, dict) else {}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _empty_async_iter() -> AsyncIterator[Any]:
    if False:
        yield None
