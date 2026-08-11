from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from z_apply_core.human.broker import BrokerRequest
from z_apply_core.integrations import CoreIntegrationConfig, RunOutcome, StartRunRequest, ZApplyCore
from z_apply_core.integrations.models import RunStatus
from z_apply_core.integrations.service import _Run
from z_apply_core.stream_events import V3RunResult


def _broker_request(
    request_id: str, created_at: datetime, *, status: str = "pending", kind: str = "question"
) -> BrokerRequest:
    return BrokerRequest(
        request_id=request_id,
        run_id="run-1",
        kind=kind,
        question="Question",
        context="Required field",
        options=("Immediate", "30 days"),
        risk="medium",
        allow_free_text=False,
        image_path="",
        created_at=created_at,
        status=status,
    )


async def _requested(core: ZApplyCore, run: _Run, request_id: str, created_at: datetime) -> None:
    await core._human_requested(run, _broker_request(request_id, created_at))


async def _resolved(
    core: ZApplyCore, run: _Run, request_id: str, created_at: datetime, *, cancelled: bool = False
) -> None:
    status = "cancelled" if cancelled else "resolved"
    await core._human_resolved(run, _broker_request(request_id, created_at, status=status))


@pytest.mark.asyncio
async def test_resolving_the_only_pending_request_returns_run_to_running() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _Run(StartRunRequest(job_url="https://example.test/job"), "run-1")
    requested_at = datetime.now(UTC)

    await _requested(core, run, "request-1", requested_at)
    assert run.view.status is RunStatus.WAITING_HUMAN
    assert run.view.pending_human_request_id == "request-1"

    await _resolved(core, run, "request-1", requested_at)

    assert run.view.status is RunStatus.RUNNING
    assert run.view.pending_human_request_id is None


@pytest.mark.asyncio
async def test_resolving_first_of_two_keeps_run_waiting_on_the_other() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _Run(StartRunRequest(job_url="https://example.test/job"), "run-1")
    first_at = datetime.now(UTC)
    second_at = first_at + timedelta(seconds=1)

    await _requested(core, run, "request-1", first_at)
    await _requested(core, run, "request-2", second_at)
    assert run.view.status is RunStatus.WAITING_HUMAN
    assert run.view.pending_human_request_id == "request-1"

    await _resolved(core, run, "request-1", first_at)

    assert run.view.status is RunStatus.WAITING_HUMAN
    assert run.view.pending_human_request_id == "request-2"

    await _resolved(core, run, "request-2", second_at)

    assert run.view.status is RunStatus.RUNNING
    assert run.view.pending_human_request_id is None


@pytest.mark.asyncio
async def test_cancelling_one_of_two_pending_leaves_the_other_waiting() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _Run(StartRunRequest(job_url="https://example.test/job"), "run-1")
    first_at = datetime.now(UTC)
    second_at = first_at + timedelta(seconds=1)

    await _requested(core, run, "request-1", first_at)
    await _requested(core, run, "request-2", second_at)

    await _resolved(core, run, "request-1", first_at, cancelled=True)

    assert run.view.status is RunStatus.WAITING_HUMAN
    assert run.view.pending_human_request_id == "request-2"

    await _resolved(core, run, "request-2", second_at)

    assert run.view.status is RunStatus.RUNNING
    assert run.view.pending_human_request_id is None


@pytest.mark.asyncio
async def test_pending_pointer_tracks_the_oldest_still_pending_request() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _Run(StartRunRequest(job_url="https://example.test/job"), "run-1")
    first_at = datetime.now(UTC)
    second_at = first_at + timedelta(seconds=1)

    await _requested(core, run, "request-1", first_at)
    await _requested(core, run, "request-2", second_at)
    await _requested(core, run, "request-3", second_at + timedelta(seconds=1))

    assert run.view.pending_human_request_id == "request-1"

    await _resolved(core, run, "request-1", first_at)
    assert run.view.pending_human_request_id == "request-2"


@pytest.mark.asyncio
async def test_service_limits_concurrent_runs_without_serializing_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum = 0
    release = asyncio.Event()

    async def fake_run_job(*args: object, **kwargs: object) -> tuple[dict[str, str], V3RunResult]:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await release.wait()
        active -= 1
        return {"run_status": "completed", "orchestrator_summary": "verified"}, V3RunResult(
            event_count=4
        )

    monkeypatch.setattr("z_apply_core.integrations.service.make_router", lambda: object())
    monkeypatch.setattr("z_apply_core.integrations.service.run_job", fake_run_job)
    monkeypatch.setattr(
        "z_apply_core.integrations.service.CandidateMemory",
        lambda: SimpleNamespace(close=lambda: None),
    )
    core = ZApplyCore(CoreIntegrationConfig(max_active_runs=2))

    async def fake_open_run(run_id: str) -> object:
        del run_id

        async def call_tool(name: str, arguments: object) -> str:
            del name, arguments
            return "navigated"

        return SimpleNamespace(
            session=SimpleNamespace(call_tool=call_tool),
            display=None,
            live_view=None,
        )

    async def fake_close_run(run_id: str) -> None:
        del run_id

    monkeypatch.setattr(core._workspace, "open_run", fake_open_run)
    monkeypatch.setattr(core._workspace, "close_run", fake_close_run)
    await core.start()
    try:
        handles = await asyncio.gather(
            *(
                core.start_run(StartRunRequest(f"https://example.test/{number}"))
                for number in range(3)
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert maximum == 2

        release.set()
        results = await asyncio.gather(*(handle.wait() for handle in handles))
        assert [result.outcome for result in results] == [RunOutcome.SUBMITTED_VERIFIED] * 3
    finally:
        await core.close()
