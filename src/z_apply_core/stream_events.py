from __future__ import annotations

import inspect
import time
import warnings
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Protocol, cast


class FrameworkEventSink(Protocol):
    async def accept(self, event: FrameworkTraceEvent) -> None:
        pass


@dataclass(slots=True)
class FrameworkTraceEvent:
    event: str
    name: str
    data: dict[str, Any]
    raw: dict[str, Any]


@dataclass(slots=True)
class V3RunResult:
    output: dict[str, Any] = field(default_factory=dict)
    event_count: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)


V3EventStream = Any


async def consume_v3_events(
    stream: V3EventStream,
    *,
    sink: FrameworkEventSink | None = None,
) -> V3RunResult:
    started = time.monotonic()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*v3 streaming protocol.*")
        resolved = await _resolve_stream(stream)
    event_count = 0
    errors: list[str] = []
    fallback_output: dict[str, Any] = {}

    async with _managed_stream(resolved) as events:
        async for raw_event in events:
            event_count += 1
            event = _normalize_event(_event_dict(raw_event))
            if event.event == "error":
                errors.append(str(event.data.get("error", event.raw)))
            fallback_output.update(_output_from_event(event))
            if sink is not None:
                await sink.accept(event)

    native_output = await _read_output(resolved)
    output = native_output or fallback_output
    return V3RunResult(
        output=output,
        event_count=event_count,
        errors=errors,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def _resolve_stream(stream: V3EventStream) -> AsyncIterator[Any]:
    if inspect.isawaitable(stream):
        resolved: Any = await stream
        return cast(AsyncIterator[Any], resolved)
    return cast(AsyncIterator[Any], stream)


@asynccontextmanager
async def _managed_stream(
    stream: AsyncIterator[Any],
) -> AsyncIterator[AsyncIterator[Any]]:
    if hasattr(stream, "__aenter__") and hasattr(stream, "__aexit__"):
        managed_stream = cast(Any, stream)
        async with managed_stream:
            yield stream
        return
    yield stream


def _event_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return {"event": "event", "data": raw}


def _normalize_event(raw: dict[str, Any]) -> FrameworkTraceEvent:
    if "method" in raw:
        method = str(raw.get("method") or "event")
        params = raw.get("params")
        data = _dict_or_value(params)
        name = _event_name(method, data)
        return FrameworkTraceEvent(event=method, name=name, data=data, raw=raw)

    event = str(raw.get("event") or "event")
    data = _dict_or_value(raw.get("data"))
    name = str(raw.get("name") or data.get("name") or event)
    return FrameworkTraceEvent(event=event, name=name, data=data, raw=raw)


def _event_name(event: str, data: dict[str, Any]) -> str:
    namespace = data.get("namespace")
    if isinstance(namespace, list | tuple) and namespace:
        return str(namespace[-1])
    return str(data.get("name") or event)


def _dict_or_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"value": value}


def _output_from_event(event: FrameworkTraceEvent) -> dict[str, Any]:
    if event.event == "values":
        return _mapping_from_value(event.data.get("data"))
    if event.event == "updates":
        data = event.data.get("data")
        if isinstance(data, Mapping):
            return _flatten_updates(data)
    if event.event == "on_chain_end":
        output = event.data.get("output")
        if isinstance(output, dict):
            return dict(output)
    return {}


def _flatten_updates(data: Mapping[Any, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for value in data.values():
        if isinstance(value, dict):
            output.update(value)
    return output


async def _read_output(stream: AsyncIterator[Any]) -> dict[str, Any]:
    output_attr = getattr(stream, "output", None)
    if output_attr is None:
        return {}
    output = output_attr() if callable(output_attr) else output_attr
    if inspect.isawaitable(output):
        output = await output
    return _mapping_from_value(output)


def _mapping_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    return {}
