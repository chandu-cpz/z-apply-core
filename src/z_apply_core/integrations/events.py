"""Bounded in-process event fan-out for transport adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from z_apply_core.integrations.models import CoreEvent


class CoreEventSink(Protocol):
    async def accept(self, event: CoreEvent) -> None: ...


class EventBroadcaster:
    def __init__(self, *, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[CoreEvent | None]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: CoreEvent) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow observer must not retain an unbounded private event history.
                with __import__("contextlib").suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with __import__("contextlib").suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    @asynccontextmanager
    async def subscription(self) -> AsyncIterator[AsyncIterator[CoreEvent]]:
        queue: asyncio.Queue[CoreEvent | None] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)

        async def iterate() -> AsyncIterator[CoreEvent]:
            while (event := await queue.get()) is not None:
                yield event

        try:
            yield iterate()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
