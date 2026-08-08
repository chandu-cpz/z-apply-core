from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from z_apply_core.integrations.events import EventBroadcaster
from z_apply_core.integrations.models import CoreEvent


def _event(sequence: int) -> CoreEvent:
    return CoreEvent(
        run_id="run-1",
        sequence=sequence,
        occurred_at=datetime.now(UTC),
        type="agent.message.delta",
        source={"component": "graph", "agent": "orchestrator"},
        level="info",
        payload={"kind": "text", "delta": f"t{sequence}"},
    )


async def _collect(broadcaster: EventBroadcaster, count: int) -> list[CoreEvent]:
    events: list[CoreEvent] = []
    async with broadcaster.subscription() as stream:
        async for event in stream:
            events.append(event)
            if len(events) == count:
                break
    return events


@pytest.mark.asyncio
async def test_late_subscriber_receives_replay_tail_then_live_events() -> None:
    broadcaster = EventBroadcaster()
    for sequence in range(1, 4):
        await broadcaster.publish(_event(sequence))

    collected = await asyncio.gather(
        _collect(broadcaster, 4),
        _publish_after_delay(broadcaster, sequence=4),
    )
    events = collected[0]

    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert len(events) == 4


@pytest.mark.asyncio
async def test_registered_subscriber_gets_events_without_duplicates() -> None:
    broadcaster = EventBroadcaster()
    for sequence in range(1, 4):
        await broadcaster.publish(_event(sequence))

    collected = await asyncio.gather(
        _collect(broadcaster, 5),
        _publish_after_delay(broadcaster, sequence=4),
        _publish_after_delay(broadcaster, sequence=5),
    )
    events = collected[0]

    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert len(events) == 5


@pytest.mark.asyncio
async def test_replay_tail_is_bounded() -> None:
    broadcaster = EventBroadcaster(replay_limit=2)
    for sequence in range(1, 6):
        await broadcaster.publish(_event(sequence))

    events = await _collect(broadcaster, 1)
    assert [event.sequence for event in events] == [4]


async def _publish_after_delay(broadcaster: EventBroadcaster, sequence: int) -> None:
    await asyncio.sleep(0.02)
    await broadcaster.publish(_event(sequence))
