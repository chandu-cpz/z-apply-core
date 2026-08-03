from __future__ import annotations

import asyncio

import pytest

from z_apply_core.human.broker import BrokerRequest, HumanRequestBroker


async def _append(items: list[BrokerRequest], request: BrokerRequest) -> None:
    items.append(request)


def _run_broker(
    requested: list[BrokerRequest] | None = None,
    resolved: list[BrokerRequest] | None = None,
) -> HumanRequestBroker:
    async def on_requested(request: BrokerRequest) -> None:
        if requested is not None:
            await _append(requested, request)

    async def on_resolved(request: BrokerRequest) -> None:
        if resolved is not None:
            await _append(resolved, request)

    return HumanRequestBroker(
        run_id="run-1",
        on_requested=on_requested,
        on_resolved=on_resolved,
    )


async def _request(broker: HumanRequestBroker, question: str, *, options: list[str] | None = None) -> asyncio.Task[str]:
    return asyncio.create_task(
        broker.request(
            kind="question",
            question=question,
            context="Required field",
            url="https://example.test/job",
            company="Example",
            role="Engineer",
            options=options or ["Immediate", "30 days"],
            risk="medium",
            image_path="",
        )
    )


@pytest.mark.asyncio
async def test_single_request_resolves_with_no_other_pending() -> None:
    requested: list[BrokerRequest] = []
    broker = _run_broker(requested=requested)

    waiting = await _request(broker, "What is your notice period?")
    await asyncio.sleep(0)
    request_id = requested[0].request_id
    await broker.resolve_answer(request_id, "Immediate", responder="web")

    assert await waiting == "Immediate"
    assert broker._pending == {}


@pytest.mark.asyncio
async def test_two_concurrent_requests_both_succeed_and_pending() -> None:
    requested: list[BrokerRequest] = []
    broker = _run_broker(requested=requested)

    first = await _request(broker, "First question")
    second = await _request(broker, "Second question")
    await asyncio.sleep(0)

    assert len(requested) == 2
    first_id, second_id = requested[0].request_id, requested[1].request_id
    assert set(broker._pending) == {first_id, second_id}

    await broker.resolve_answer(first_id, "Immediate", responder="web")
    await broker.resolve_answer(second_id, "30 days", responder="web")

    assert await first == "Immediate"
    assert await second == "30 days"
    assert broker._pending == {}


@pytest.mark.asyncio
async def test_on_requested_fires_in_fifo_creation_order() -> None:
    requested: list[BrokerRequest] = []
    broker = _run_broker(requested=requested)

    first = await _request(broker, "First question")
    second = await _request(broker, "Second question")
    await asyncio.sleep(0)

    assert [request.question for request in requested] == ["First question", "Second question"]
    assert list(broker._pending) == [requested[0].request_id, requested[1].request_id]

    await broker.resolve_answer(requested[0].request_id, "Immediate", responder="web")
    await broker.resolve_answer(requested[1].request_id, "30 days", responder="web")
    assert await first == "Immediate"
    assert await second == "30 days"


@pytest.mark.asyncio
async def test_each_request_resolves_independently_by_own_request_id() -> None:
    requested: list[BrokerRequest] = []
    broker = _run_broker(requested=requested)

    first = await _request(broker, "First question")
    second = await _request(broker, "Second question")
    await asyncio.sleep(0)
    first_id, second_id = requested[0].request_id, requested[1].request_id

    await broker.resolve_answer(first_id, "Immediate", responder="web")

    assert await first == "Immediate"
    assert first_id not in broker._pending
    assert second_id in broker._pending

    await broker.resolve_answer(second_id, "30 days", responder="web")
    assert await second == "30 days"
    assert broker._pending == {}


@pytest.mark.asyncio
async def test_cancel_of_one_request_does_not_affect_the_other() -> None:
    requested: list[BrokerRequest] = []
    resolved: list[BrokerRequest] = []
    broker = _run_broker(requested=requested, resolved=resolved)

    first = await _request(broker, "First question")
    second = await _request(broker, "Second question")
    await asyncio.sleep(0)
    first_id, second_id = requested[0].request_id, requested[1].request_id

    await broker.cancel(first_id)

    assert first_id not in broker._pending
    assert second_id in broker._pending
    assert {entry.request_id for entry in resolved} == {first_id}
    with pytest.raises(asyncio.CancelledError):
        await first

    await broker.resolve_answer(second_id, "30 days", responder="web")
    assert await second == "30 days"
    assert broker._pending == {}


@pytest.mark.asyncio
async def test_unknown_request_id_still_raises_key_error() -> None:
    broker = _run_broker()

    with pytest.raises(KeyError):
        await broker.resolve_answer("does-not-exist", "Immediate", responder="web")
