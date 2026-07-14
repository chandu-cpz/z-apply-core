from __future__ import annotations

import asyncio

import pytest

from z_apply_core.integrations import CoreIntegrationConfig, RunOutcome, StartRunRequest, ZApplyCore
from z_apply_core.stream_events import V3RunResult


@pytest.mark.asyncio
async def test_service_limits_concurrent_runs_without_serializing_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    maximum = 0
    release = asyncio.Event()

    async def fake_run_job(*args: object, **kwargs: object) -> tuple[dict[str, str], V3RunResult]:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await release.wait()
        active -= 1
        return {"run_status": "completed", "orchestrator_summary": "verified"}, V3RunResult(event_count=4)

    monkeypatch.setattr("z_apply_core.integrations.service.make_router", lambda: object())
    monkeypatch.setattr("z_apply_core.integrations.service.run_job", fake_run_job)
    core = ZApplyCore(CoreIntegrationConfig(max_active_runs=2))
    await core.start()
    try:
        handles = await asyncio.gather(
            *(core.start_run(StartRunRequest(f"https://example.test/{number}")) for number in range(3))
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert maximum == 2

        release.set()
        results = await asyncio.gather(*(handle.wait() for handle in handles))
        assert [result.outcome for result in results] == [RunOutcome.SUBMITTED_VERIFIED] * 3
    finally:
        await core.close()
