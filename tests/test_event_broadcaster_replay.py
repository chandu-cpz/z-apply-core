from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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


@pytest.mark.asyncio
async def test_keepalive_yields_none_markers_while_silent() -> None:
    broadcaster = EventBroadcaster()

    markers: list[CoreEvent | None] = []
    async with broadcaster.subscription(keepalive=0.02) as stream:
        async for event in stream:
            markers.append(event)
            if event is None:
                break

    assert markers == [None]
    # A real event still flows after a keepalive marker on the same iterator.
    received: list[CoreEvent | None] = []

    async def _publish_then_collect() -> None:
        await asyncio.sleep(0.04)  # let one keepalive fire first
        await broadcaster.publish(_event(7))

    async with broadcaster.subscription(keepalive=0.02) as stream:
        await asyncio.gather(
            _publish_then_collect(),
            _collect_until(stream, received, stop_at_sequence=7),
        )
    assert [event.sequence for event in received if event is not None] == [7]


async def _collect_until(
    stream: AsyncIterator[CoreEvent | None],
    received: list[CoreEvent | None],
    *,
    stop_at_sequence: int,
) -> None:
    async for event in stream:
        received.append(event)
        if event is not None and event.sequence == stop_at_sequence:
            return


@pytest.mark.asyncio
async def test_slow_subscriber_drops_are_counted_and_reported() -> None:
    broadcaster = EventBroadcaster(queue_size=1)
    async with broadcaster.subscription() as stream:
        for sequence in range(1, 6):
            await broadcaster.publish(_event(sequence))
        # queue_size=1: only the newest survives; the other 4 were dropped.
        event = await asyncio.wait_for(anext(stream), timeout=0.1)
        assert event is not None and event.sequence == 5

    snapshot = await broadcaster.snapshot()
    assert snapshot["subscribers"] == 0
    assert snapshot["dropped_events"] == 0  # subscriber disconnected
    assert snapshot["total_dropped_events"] == 4
    assert snapshot["replay_size"] == 5
    assert snapshot["replay_newest_seq"] == 5


@pytest.mark.asyncio
async def test_snapshot_reports_active_subscribers_and_replay() -> None:
    broadcaster = EventBroadcaster()
    await broadcaster.publish(_event(1))

    async with broadcaster.subscription() as stream:
        snapshot = await broadcaster.snapshot()
        assert snapshot["subscribers"] == 1
        assert snapshot["replay_size"] == 1
        assert snapshot["replay_newest_seq"] == 1
        # Close the subscription so the loop exits.
        await stream.aclose()  # type: ignore[attr-defined]
        await asyncio.sleep(0)

    snapshot = await broadcaster.snapshot()
    assert snapshot["subscribers"] == 0


def _message_delta(sequence: int, kind: str, delta: str, agent: str = "researcher") -> CoreEvent:
    return CoreEvent(
        run_id="run-1",
        sequence=sequence,
        occurred_at=datetime.now(UTC),
        type="agent.message.delta",
        source={"component": "graph", "agent": agent},
        level="info",
        payload={"kind": kind, "delta": delta, "message_id": f"{agent}:turn-1", "turn_index": 1},
    )


def _tool_delta(sequence: int, index: int, args: str, agent: str = "researcher") -> CoreEvent:
    return CoreEvent(
        run_id="run-1",
        sequence=sequence,
        occurred_at=datetime.now(UTC),
        type="model.tool_call.delta",
        source={"component": "graph", "agent": agent},
        level="info",
        payload={"index": index, "id": "call_1", "name": "search_jobs", "args": args},
    )


def _turn_completed(sequence: int, agent: str = "researcher") -> CoreEvent:
    return CoreEvent(
        run_id="run-1",
        sequence=sequence,
        occurred_at=datetime.now(UTC),
        type="agent.turn.completed",
        source={"component": "graph", "agent": agent},
        level="info",
        payload={"agent": agent, "text": "final"},
    )


async def _collect_frames(broadcaster: EventBroadcaster, count: int) -> list[CoreEvent | None]:
    frames: list[CoreEvent | None] = []
    async with broadcaster.subscription(coalesce=True) as stream:
        async for event in stream:
            frames.append(event)
            if len(frames) == count:
                break
    return frames


@pytest.mark.asyncio
async def test_coalesce_merges_consecutive_message_deltas() -> None:
    broadcaster = EventBroadcaster()
    await broadcaster.publish(_message_delta(1, "text", "stream"))
    await broadcaster.publish(_message_delta(2, "text", "ing "))
    await broadcaster.publish(_message_delta(3, "text", "markdown"))
    await broadcaster.publish(_turn_completed(4))

    frames = await _collect_frames(broadcaster, 2)
    merged, boundary = frames[0], frames[1]

    assert merged is not None and merged.type == "agent.message.delta"
    assert merged.payload["deltas"] == ["stream", "ing ", "markdown"]
    assert merged.payload["kind"] == "text"
    assert merged.sequence == 3  # newest merged delta
    assert boundary is not None and boundary.type == "agent.turn.completed"


@pytest.mark.asyncio
async def test_coalesce_keeps_kinds_and_agents_separate() -> None:
    broadcaster = EventBroadcaster()
    await broadcaster.publish(_message_delta(1, "reasoning", "think"))
    await broadcaster.publish(_message_delta(2, "text", "answer"))
    await broadcaster.publish(_message_delta(3, "text", " two", agent="browser-specialist"))
    await broadcaster.publish(_turn_completed(4))

    frames = await _collect_frames(broadcaster, 4)
    assert frames[0].payload["deltas"] == ["think"]  # type: ignore[union-attr]
    assert frames[1].payload["deltas"] == ["answer"]  # type: ignore[union-attr]
    assert frames[2].payload["deltas"] == [" two"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_coalesce_merges_tool_call_args_per_index() -> None:
    broadcaster = EventBroadcaster()
    await broadcaster.publish(_tool_delta(1, 0, '{"q":'))
    await broadcaster.publish(_tool_delta(2, 0, '"eng"}'))
    await broadcaster.publish(_tool_delta(3, 1, "{"))
    await broadcaster.publish(_turn_completed(4))

    frames = await _collect_frames(broadcaster, 3)
    first = frames[0]
    assert first.payload["args_deltas"] == ['{"q":', '"eng"}']  # type: ignore[union-attr]
    assert first.payload["index"] == 0  # type: ignore[union-attr]
    assert frames[1].payload["args_deltas"] == ["{"]  # type: ignore[union-attr]
    assert frames[1].payload["index"] == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_coalesce_flushes_frame_by_delta_count() -> None:
    broadcaster = EventBroadcaster()
    from z_apply_core.integrations import events as events_module

    limit = events_module._MAX_DELTAS_PER_FRAME
    for sequence in range(1, limit + 3):
        await broadcaster.publish(_message_delta(sequence, "text", "x"))
    await broadcaster.publish(_turn_completed(999))  # flushes the partial frame

    frames = await _collect_frames(broadcaster, 3)
    assert frames[0].payload["deltas"] == ["x"] * limit  # type: ignore[union-attr]
    assert frames[1].payload["deltas"] == ["x", "x"]  # type: ignore[union-attr]
    assert frames[2].type == "agent.turn.completed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_coalesce_flushes_frame_by_time_and_keeps_streaming() -> None:
    broadcaster = EventBroadcaster()
    await broadcaster.publish(_message_delta(1, "text", "a"))

    collected: list[CoreEvent | None] = []
    async with broadcaster.subscription(coalesce=True, keepalive=0.05) as stream:
        async for event in stream:
            collected.append(event)
            if event is None:
                continue
            if event.type != "agent.message.delta":
                continue
            # 60ms flush threshold: the time-based flush yields the frame.
            if event.payload.get("deltas") == ["a"]:
                await asyncio.sleep(0.08)
                await broadcaster.publish(_message_delta(2, "text", "b"))
                continue
            if event.payload.get("deltas") == ["b"]:
                break

    merged = [event for event in collected if event is not None and event.type == "agent.message.delta"]
    assert [event.payload.get("deltas") for event in merged] == [["a"], ["b"]]


@pytest.mark.asyncio
async def test_stream_metrics_flows_through_live_only_route() -> None:
    """model_call_progress maps to stream.metrics and is live-only (not durable)."""
    from z_apply_core.integrations.service import _LIVE_ONLY_EVENTS, _typed_framework_event

    assert "stream.metrics" in _LIVE_ONLY_EVENTS
    payload = {
        "role": "researcher",
        "provider": "opengateway",
        "model": "deepseek/v3",
        "ttft_ms": 180,
        "tok_per_second": 42.5,
        "output_tokens_estimate": 640,
        "duration_ms": 15200,
    }
    assert _typed_framework_event("model_call_progress", payload) == "stream.metrics"


def test_model_call_metrics_maps_to_durable_ledger_event() -> None:
    """model_call_metrics ships per-call ledger data durably (cost + tok/s)."""
    from z_apply_core.integrations.service import _typed_framework_event

    payload = {
        "role": "researcher",
        "model_id": "deepseek/v3",
        "provider": "opengateway",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_tokens": 800,
        "ttft_ms": 180,
        "duration_ms": 15200,
        "tok_per_second": 22.4,
        "cost_usd": 0.001234,
    }
    assert _typed_framework_event("model_call_metrics", payload) == "model.call.metrics"
