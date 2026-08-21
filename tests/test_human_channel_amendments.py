"""DEC-002 amendment proofs for the human-channel layer.

A1: a broker request must reach BOTH channels — the web cockpit hook and the
Telegram transport. A2: an unanswered request must block its asker until it is
resolved (the run may not proceed on missing candidate facts).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from z_apply_core.human.broker import BrokerRequest, HumanRequestBroker


class _FakeTelegram:
    """Records the fan-out and blocks like a waiting Telegram chat.

    Implements exactly what HumanRequestBroker touches: ``ask_with_id`` when
    the transport task starts, ``cancel_request`` when the web channel answers
    first (first-response-wins) or the broker closes.
    """

    def __init__(self) -> None:
        self.asked_with: dict[str, Any] = {}
        self.cancelled: list[str] = []
        self._release = asyncio.Event()

    async def ask_with_id(self, **kwargs: Any) -> str:
        self.asked_with[kwargs["request_id"]] = kwargs
        await self._release.wait()
        return "unreachable"

    async def cancel_request(self, request_id: str) -> None:
        self.cancelled.append(request_id)
        self._release.set()


def _broker(
    requested: list[BrokerRequest],
    resolved: list[BrokerRequest],
    telegram: _FakeTelegram | None,
) -> HumanRequestBroker:
    async def on_requested(request: BrokerRequest) -> None:
        requested.append(request)

    async def on_resolved(request: BrokerRequest) -> None:
        resolved.append(request)

    return HumanRequestBroker(
        run_id="run-dec002",
        on_requested=on_requested,
        on_resolved=on_resolved,
        telegram=telegram,
    )


@pytest.mark.asyncio
async def test_request_fans_out_to_web_and_telegram_channels() -> None:
    requested: list[BrokerRequest] = []
    resolved: list[BrokerRequest] = []
    telegram = _FakeTelegram()
    broker = _broker(requested, resolved, telegram)

    waiting = asyncio.create_task(
        broker.request(
            kind="question",
            question="Expected compensation?",
            context="Free-text screening question",
            url="https://example.test/job",
            company="Example",
            role="Engineer",
            options=[],
            risk="low",
            image_path="",
            field_label="expected compensation",
            reason="missing_candidate_fact",
        )
    )
    for _ in range(4):
        await asyncio.sleep(0)

    # Web cockpit received it...
    assert len(requested) == 1
    request_id = requested[0].request_id
    # ...and the Telegram transport got the same question.
    assert request_id in telegram.asked_with
    assert telegram.asked_with[request_id]["question"] == "Expected compensation?"

    await broker.resolve_answer(request_id, "42", responder="web")
    assert await waiting == "42"
    assert len(resolved) == 1
    assert resolved[0].status == "resolved"
    assert telegram.cancelled == [request_id]
    await broker.close()


@pytest.mark.asyncio
async def test_pending_request_blocks_asker_until_resolved() -> None:
    requested: list[BrokerRequest] = []
    resolved: list[BrokerRequest] = []
    broker = _broker(requested, resolved, None)

    waiting = asyncio.create_task(
        broker.request(
            kind="question",
            question="Notice period?",
            context="Free-text screening question",
            url="https://example.test/job",
            company="Example",
            role="Engineer",
            options=[],
            risk="low",
            image_path="",
            field_label="notice period",
            reason="missing_candidate_fact",
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not waiting.done(), "asker must stay blocked while the request is pending"

    request_id = requested[0].request_id
    await broker.resolve_answer(request_id, "30 days", responder="web")

    assert await asyncio.wait_for(waiting, timeout=1) == "30 days"
    await broker.close()
