"""Bounded in-process event fan-out for transport adapters."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from z_apply_core.integrations.models import CoreEvent


class CoreEventSink(Protocol):
    async def accept(self, event: CoreEvent) -> None: ...


# Delta event types that may be coalesced into batched frames on the wire.
_COALESCIBLE_EVENTS = frozenset({"agent.message.delta", "model.tool_call.delta"})

# Coalescing flush policies: a frame is emitted when any threshold is hit.
_MAX_DELTAS_PER_FRAME = 64
_MAX_FRAME_BYTES = 32 * 1024
_FRAME_FLUSH_SECONDS = 0.06


@dataclass(slots=True)
class _DeltaFrame:
    """Accumulate consecutive wire deltas into one batched CoreEvent.

    Deltas only merge when they share a merge key (same run, agent, and
    message kind / tool-call index), so the batched frame is semantically
    identical to its parts. The frame's sequence/occurred_at advance to the
    newest merged delta, preserving monotonic ordering.
    """

    run_id: str
    source: Mapping[str, str]
    level: str
    event_type: str
    merge_key: tuple[Any, ...]
    sequence: int
    occurred_at: datetime
    base: dict[str, Any]
    deltas: list[str]
    count: int = 0
    bytes: int = 0
    started: float = field(default_factory=time.monotonic)


def _frame_from_event(event: CoreEvent) -> _DeltaFrame | None:
    """Start a frame from one delta event, or None for a non-mergeable shape."""
    payload = event.payload
    if event.type == "agent.message.delta":
        delta = payload.get("delta")
        if not isinstance(delta, str):
            return None
        kind = str(payload.get("kind") or "text")
        return _DeltaFrame(
            run_id=event.run_id,
            source=event.source,
            level=event.level,
            event_type=event.type,
            merge_key=(event.run_id, event.source.get("agent"), "message", kind),
            sequence=event.sequence,
            occurred_at=event.occurred_at,
            base={
                "kind": kind,
                "message_id": payload.get("message_id"),
                "turn_index": payload.get("turn_index"),
            },
            deltas=[delta],
            count=1,
            bytes=len(delta) + 32,
        )
    if event.type == "model.tool_call.delta":
        index = payload.get("index")
        if not isinstance(index, int):
            return None
        args = payload.get("args")
        if not isinstance(args, str):
            return None
        return _DeltaFrame(
            run_id=event.run_id,
            source=event.source,
            level=event.level,
            event_type=event.type,
            merge_key=(event.run_id, event.source.get("agent"), "tool", index),
            sequence=event.sequence,
            occurred_at=event.occurred_at,
            base={
                "index": index,
                "id": str(payload.get("id") or ""),
                "name": str(payload.get("name") or ""),
            },
            deltas=[args],
            count=1,
            bytes=len(args) + 32,
        )
    return None


def _frame_append(frame: _DeltaFrame, event: CoreEvent) -> None:
    """Merge one delta into an existing frame (same merge key, by construction)."""
    if frame.event_type == "agent.message.delta":
        delta = event.payload.get("delta")
    else:
        delta = event.payload.get("args")
    if not isinstance(delta, str):
        return
    frame.deltas.append(delta)
    frame.bytes += len(delta) + 8
    frame.count += 1
    frame.sequence = event.sequence
    frame.occurred_at = event.occurred_at


def _frame_full(frame: _DeltaFrame) -> bool:
    return frame.count >= _MAX_DELTAS_PER_FRAME or frame.bytes >= _MAX_FRAME_BYTES


def _flush_frame(frame: _DeltaFrame) -> CoreEvent:
    payload = dict(frame.base)
    if frame.event_type == "agent.message.delta":
        payload["deltas"] = frame.deltas
    else:
        payload["args_deltas"] = frame.deltas
    return CoreEvent(
        run_id=frame.run_id,
        sequence=frame.sequence,
        occurred_at=frame.occurred_at,
        type=frame.event_type,
        source=frame.source,
        level=frame.level,
        payload=payload,
    )


class EventBroadcaster:
    """Bounded fan-out with a short replay tail for late subscribers.

    Live deltas are published here without persistence; a bounded replay tail
    lets a cockpit reconnect or reload and reconstruct an in-flight stream
    instead of losing every token published before the subscription opened.
    """

    def __init__(
        self,
        *,
        queue_size: int = 256,
        replay_limit: int = 500,
        drop_counter: bool = True,
    ) -> None:
        self._queue_size = queue_size
        self._replay_limit = replay_limit
        self._replay: deque[CoreEvent] = deque(maxlen=replay_limit)
        self._subscribers: set[asyncio.Queue[CoreEvent | None]] = set()
        self._drops: dict[asyncio.Queue[CoreEvent | None], int] = {}
        self._total_drops = 0
        self._lock = asyncio.Lock()
        self._drop_counter = drop_counter

    async def publish(self, event: CoreEvent) -> None:
        async with self._lock:
            self._replay.append(event)
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow observer must not retain an unbounded private event
                # history. Drop the oldest waiting delta (tokens are ephemeral:
                # the next delta supersedes it) and count the drop so transport
                # adapters can surface backpressure instead of silently losing
                # tokens.
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(event)
                if self._drop_counter:
                    async with self._lock:
                        self._drops[queue] = self._drops.get(queue, 0) + 1
                        self._total_drops += 1

    @asynccontextmanager
    async def subscription(
        self,
        *,
        keepalive: float | None = None,
        coalesce: bool = False,
    ) -> AsyncIterator[AsyncIterator[CoreEvent | None]]:
        """Subscribe with optional keepalive and delta coalescing.

        ``keepalive`` (seconds): when no event arrives within the window the
        iterator yields ``None`` so transport adapters can emit an SSE comment
        and keep proxy/load-balancer idle timeouts from killing an
        otherwise-silent stream (long tool executions produce no token deltas).

        ``coalesce``: consecutive ``agent.message.delta`` / ``model.tool_call.delta``
        events from the same agent (and same kind / tool-call index) are merged
        into batched frames carrying ``deltas: [...]`` / ``args_deltas: [...]``.
        Frames flush at 60ms, 64 deltas, or 32KB — whichever comes first — and
        always before any non-delta event, so turn boundaries, tool starts, and
        metrics are never merged into a token frame. This cuts SSE frame count
        ~10-20x for fast models and aligns the wire with the browser's
        animation-frame rendering cadence.
        """
        queue: asyncio.Queue[CoreEvent | None] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            # Registration and the replay snapshot share one critical section
            # with publish's append+collect, so an event is delivered exactly
            # once: either in the replay tail (appended before registration) or
            # through the live queue (appended after registration), never both.
            self._subscribers.add(queue)
            replay = tuple(self._replay)

        async def iterate() -> AsyncIterator[CoreEvent | None]:
            frame: _DeltaFrame | None = None

            def emit_frame() -> CoreEvent | None:
                nonlocal frame
                if frame is None:
                    return None
                flushed = _flush_frame(frame)
                frame = None
                return flushed

            def handle_delta(event: CoreEvent) -> CoreEvent | None:
                """Merge into the pending frame; returns a frame to emit, if any."""
                nonlocal frame
                candidate = _frame_from_event(event)
                if candidate is None:
                    flushed = emit_frame()
                    return flushed if flushed is not None else event
                if frame is not None and frame.merge_key == candidate.merge_key:
                    _frame_append(frame, event)
                    if _frame_full(frame):
                        return emit_frame()
                    return None
                flushed = emit_frame()
                frame = candidate
                return flushed

            for replay_event in replay:
                if coalesce and replay_event.type in _COALESCIBLE_EVENTS:
                    emitted = handle_delta(replay_event)
                    if emitted is not None:
                        yield emitted
                else:
                    flushed = emit_frame()
                    if flushed is not None:
                        yield flushed
                    yield replay_event

            while True:
                timeout: float | None = None
                if keepalive is not None or frame is not None:
                    candidates: list[float] = []
                    if keepalive is not None:
                        candidates.append(keepalive)
                    if frame is not None:
                        elapsed = time.monotonic() - frame.started
                        candidates.append(max(0.0, _FRAME_FLUSH_SECONDS - elapsed))
                    timeout = min(candidates)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    if frame is not None and time.monotonic() - frame.started >= _FRAME_FLUSH_SECONDS:
                        flushed = emit_frame()
                        if flushed is not None:
                            yield flushed
                        continue
                    yield None
                    continue
                if event is None:
                    flushed = emit_frame()
                    if flushed is not None:
                        yield flushed
                    return
                if coalesce and event.type in _COALESCIBLE_EVENTS:
                    emitted = handle_delta(event)
                    if emitted is not None:
                        yield emitted
                else:
                    flushed = emit_frame()
                    if flushed is not None:
                        yield flushed
                    yield event

        try:
            yield iterate()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
                self._drops.pop(queue, None)

    async def snapshot(self) -> dict[str, object]:
        """Observability snapshot: subscriber count, drops, replay age."""
        async with self._lock:
            subscribers = len(self._subscribers)
            active_drops = sum(self._drops.values())
            total_drops = self._total_drops
            replay_len = len(self._replay)
            newest = self._replay[-1] if self._replay else None
        return {
            "subscribers": subscribers,
            "dropped_events": active_drops,
            "total_dropped_events": total_drops,
            "replay_limit": self._replay_limit,
            "replay_size": replay_len,
            "replay_newest_seq": newest.sequence if newest is not None else None,
        }
