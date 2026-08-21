"""Every persisted event carries a fresh run-view snapshot, and every view
mutation is observable: no silent state changes on the stream."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from z_apply_core.integrations import CoreIntegrationConfig, RunOutcome, StartRunRequest, ZApplyCore
from z_apply_core.integrations import service as service_module
from z_apply_core.integrations.models import BrowserTabState, RunStatus
from z_apply_core.integrations.service import _Run, _view_snapshot


def _make_run(run_id: str = "run-1") -> _Run:
    return _Run(StartRunRequest(job_url="https://example.test/job"), run_id)


async def _collect(core: ZApplyCore, run_id: str, count: int) -> list:
    stream = core.subscribe(run_id=run_id)
    iterator = stream.__aiter__()
    return [await anext(iterator) for _ in range(count)]


@pytest.mark.asyncio
async def test_every_persisted_event_carries_a_view_snapshot() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _make_run()
    core._runs[run.run_id] = run

    events = asyncio.ensure_future(_collect(core, run.run_id, 2))
    await asyncio.sleep(0)
    await core._emit(run, "run.queued", {"job_url": run.request.job_url})
    run.view = replace(run.view, current_agent="Orchestrator")
    await core._emit(run, "agent.started", {"agent": "Orchestrator"})
    emitted = await events

    for event in emitted:
        assert isinstance(event.payload.get("view"), dict), event.type
    first, second = emitted
    assert first.payload["view"]["status"] == "queued"
    # Snapshot is taken after the mutation that triggered the event.
    assert second.payload["view"]["current_agent"] == "Orchestrator"


def test_snapshot_truncates_summary() -> None:
    run = _make_run()
    run.view = replace(run.view, summary="x" * 5_000)
    assert len(_view_snapshot(run.view)["summary"]) == 280


async def _terminal_run_with_open_browser() -> tuple[ZApplyCore, _Run, list]:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _make_run()
    core._runs[run.run_id] = run
    run.view = replace(run.view, status=RunStatus.TERMINAL, browser_tab_state=BrowserTabState.OPEN)

    async def fake_close_run(run_id: str) -> None:
        return None

    core._workspace.close_run = fake_close_run  # type: ignore[method-assign]
    events = asyncio.ensure_future(_collect(core, run.run_id, 1))
    await asyncio.sleep(0)
    return core, run, events


@pytest.mark.asyncio
async def test_retained_browser_auto_release_emits_browser_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module, "RETAINED_BROWSER_TTL_SECONDS", 0)
    core, run, events = await _terminal_run_with_open_browser()

    await core._schedule_retained_browser_release(run, RunOutcome.BLOCKED)
    assert run.retention_release is not None
    await run.retention_release
    emitted = await events

    assert emitted[0].type == "browser.closed"
    assert emitted[0].payload["view"]["browser_tab_state"] == "closed"


@pytest.mark.asyncio
async def test_cancelling_a_retained_browser_emits_browser_closed() -> None:
    core, run, events = await _terminal_run_with_open_browser()

    await core._schedule_retained_browser_release(run, RunOutcome.CANCELLED)
    emitted = await events

    assert emitted[0].type == "browser.closed"
    assert emitted[0].payload["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_shutdown_browser_workspace_emits_browser_closed_per_run() -> None:
    core = ZApplyCore(CoreIntegrationConfig())
    run = _make_run()
    core._runs[run.run_id] = run
    run.view = replace(run.view, browser_tab_state=BrowserTabState.OPEN)

    async def fake_close() -> None:
        return None

    core._workspace.close = fake_close  # type: ignore[method-assign]
    events = asyncio.ensure_future(_collect(core, run.run_id, 1))
    await asyncio.sleep(0)

    await core.shutdown_browser_workspace(force=True)
    emitted = await events

    assert emitted[0].type == "browser.closed"
    assert run.view.browser_tab_state is BrowserTabState.CLOSED
