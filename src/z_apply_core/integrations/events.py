"""Bounded in-process event fan-out for transport adapters."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Protocol

from z_apply_core.integrations.models import CoreEvent


class CoreEventSink(Protocol):
    async def accept(self, event: CoreEvent) -> None: ...


class EventBroadcaster:
    """Bounded fan-out with a short replay tail for late subscribers.

    Live deltas are published here without persistence; a bounded replay tail
    lets a cockpit reconnect or reload and reconstruct an in-flight stream
    instead of losing every token published before the subscription opened.
    """

    def __init__(self, *, queue_size: int = 256, replay_limit: int = 500) -> None:
        self._queue_size = queue_size
        self._replay_limit = replay_limit
        self._replay: deque[CoreEvent] = deque(maxlen=replay_limit)
        self._subscribers: set[asyncio.Queue[CoreEvent | None]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: CoreEvent) -> None:
        async with self._lock:
            self._replay.append(event)
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow observer must not retain an unbounded private event history.
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    @asynccontextmanager
    async def subscription(self) -> AsyncIterator[AsyncIterator[CoreEvent]]:
        queue: asyncio.Queue[CoreEvent | None] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            # Registration and the replay snapshot share one critical section
            # with publish's append+collect, so an event is delivered exactly
            # once: either in the replay tail (appended before registration) or
            # through the live queue (appended after registration), never both.
            self._subscribers.add(queue)
            replay = tuple(self._replay)

        async def iterate() -> AsyncIterator[CoreEvent]:
            for replay_event in replay:
                yield replay_event
            while (event := await queue.get()) is not None:
                yield event

        try:
            yield iterate()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
