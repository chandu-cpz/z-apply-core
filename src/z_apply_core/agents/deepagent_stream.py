from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any, cast

from langchain_core.language_models.chat_model_stream import AsyncProjection
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from z_apply_core.stream_events import (
    FrameworkEventSink,
    FrameworkTraceEvent,
    V3RunResult,
    _managed_stream,
    _read_output,
    _resolve_stream,
)

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
    turn_index = 0
    async for message_stream in messages:
        turn_index += 1
        message_id = f"{source}:turn-{turn_index}"
        turn_started = time.monotonic()
        turn: dict[str, Any] = {
            "text": "",
            "reasoning": "",
            "tool_calls": [],
            "first_delta_seen": None,
        }
        await asyncio.gather(
            _consume_message_text(source, message_stream.text, sink, turn, message_id, turn_index),
            _consume_message_reasoning(
                source, message_stream.reasoning, sink, turn, message_id, turn_index
            ),
            _consume_message_tool_call_chunks(
                source, message_stream.tool_calls, sink, turn, message_id, turn_index
            ),
            _read_message_output(message_stream),
        )
        text = turn["text"]
        reasoning = turn["reasoning"]
        tool_calls = [call for call in turn["tool_calls"] if call.get("name") or call.get("id")]
        if not text and not reasoning and not tool_calls:
            continue
        first_delta_seen = turn["first_delta_seen"]
        ttft_ms = 0 if first_delta_seen is None else int((first_delta_seen - turn_started) * 1000)
        await _emit(
            sink,
            "agent_turn",
            source,
            {
                "agent": source,
                "text": text,
                "reasoning": reasoning,
                "tool_calls": tool_calls,
                "duration_ms": int((time.monotonic() - turn_started) * 1000),
                "ttft_ms": ttft_ms,
            },
        )


async def _consume_message_text(
    source: str,
    text: AsyncIterable[str],
    sink: FrameworkEventSink | None,
    turn: dict[str, Any],
    message_id: str,
    turn_index: int,
) -> None:
    async for delta in text:
        if not delta:
            continue
        turn["text"] += delta
        if turn["first_delta_seen"] is None:
            turn["first_delta_seen"] = time.monotonic()
        await _emit(
            sink,
            "agent_message_delta",
            source,
            {
                "kind": "text",
                "delta": delta,
                "message_id": message_id,
                "turn_index": turn_index,
            },
        )


async def _consume_message_reasoning(
    source: str,
    reasoning: AsyncIterable[str],
    sink: FrameworkEventSink | None,
    turn: dict[str, Any],
    message_id: str,
    turn_index: int,
) -> None:
    async for delta in reasoning:
        if not delta:
            continue
        turn["reasoning"] += delta
        if turn["first_delta_seen"] is None:
            turn["first_delta_seen"] = time.monotonic()
        await _emit(
            sink,
            "agent_message_delta",
            source,
            {
                "kind": "reasoning",
                "delta": delta,
                "message_id": message_id,
                "turn_index": turn_index,
            },
        )


async def _consume_message_tool_call_chunks(
    source: str,
    tool_calls: AsyncProjection,
    sink: FrameworkEventSink | None,
    turn: dict[str, Any],
    message_id: str,
    turn_index: int,
) -> None:
    # Live UI: relay the raw chunk dicts the framework pushes onto the
    # ``message_stream.tool_calls`` projection (``ToolCallChunk`` shape with
    # ``id`` / ``name`` / ``args`` keys). The framework already merges
    # sticky id/name and concatenates args fragments; the chunks are plain
    # dicts, so read them with ``.get`` — not ``getattr``.
    async for chunk in tool_calls:
        if not isinstance(chunk, dict):
            continue
        chunk_data = {
            "index": int(chunk.get("index") or 0),
            "id": str(chunk.get("id") or ""),
            "name": str(chunk.get("name") or ""),
            "args": str(chunk.get("args") or ""),
        }
        # Some providers announce a tool call with a placeholder chunk whose
        # id/name/args are all empty (the real invocation arrives through the
        # executed-tool stream). Recording it pollutes the live tool-call
        # list with a nameless entry, so skip completely empty chunks.
        if not chunk_data["id"] and not chunk_data["name"] and not chunk_data["args"]:
            continue
        chunk_data["message_id"] = message_id
        chunk_data["turn_index"] = turn_index
        await _emit(
            sink,
            "agent_model_tool_call",
            source,
            chunk_data,
        )

    # Record: await the same projection for the framework-finalized list of
    # ``ToolCall`` dicts — sticky id/name, concatenated args parsed as JSON,
    # and malformed args marked as ``invalid_tool_call`` are all handled by
    # langchain-core's chunk-store sweep at ``message-finish``. This replaces
    # the hand-rolled merge that previously produced empty ``{id, name,
    # args}`` entries.
    finalized = await tool_calls
    turn["tool_calls"] = finalized or []


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
        tool_call_id = str(getattr(call, "tool_call_id", getattr(call, "id", "")))
        parent_tool_call_id = str(getattr(call, "parent_tool_call_id", ""))
        await _emit(
            sink,
            "agent_tool_start",
            source,
            {
                "tool_name": tool_name,
                "input": call.input,
                "input_full": _public_tool_output(call.input),
                "tool_call_id": tool_call_id,
                "parent_tool_call_id": parent_tool_call_id,
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
                "output": _public_tool_output(call.output),
                "output_full": _public_tool_output(call.output),
                "error": str(call.error) if call.error is not None else "",
                "completed": call.completed,
                "tool_call_id": tool_call_id,
                "parent_tool_call_id": parent_tool_call_id,
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
                "tool_call_id": tool_call_id,
                "parent_tool_call_id": parent_tool_call_id,
            }
        )


def _public_tool_output(value: Any) -> Any:
    """Normalize authoritative tool results without serializing runtime objects."""
    if isinstance(value, ToolMessage):
        return {
            "content": _public_tool_output(value.content),
            "status": value.status,
        }
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {str(key): _public_tool_output(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_tool_output(item) for item in value]
    if isinstance(value, Command):
        # LangGraph tool-results often come back as a Command that carries the
        # authoritative message(s) in ``update["messages"]`` (for example the
        # candidate-field receipts the executor rewrites into a Command). Unwrap
        # them so the stream renders the typed verdict (CANDIDATE_FIELD_* /
        # CANDIDATE_FIELD_EXECUTION_ERROR) instead of an opaque ``None``.
        update = getattr(value, "update", None)
        messages = update.get("messages", []) if isinstance(update, dict) else []
        parts = [part for part in (_public_tool_output(m) for m in messages) if part is not None]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else parts
    return None


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


def _projection(source: Any, name: str) -> AsyncIterable[Any]:
    value = getattr(source, name, None)
    if value is None:
        return _empty_async_iter()
    return cast(AsyncIterable[Any], value)


async def _empty_async_iter() -> AsyncIterator[Any]:
    if False:
        yield None
