from __future__ import annotations

from z_apply_core.agents.orchestrator import run_orchestrator
from z_apply_core.state import RunState


async def orchestrator(state: RunState) -> dict[str, str]:
    run = await run_orchestrator(
        job_url=str(state["job_url"]),
        task=str(state["task"]),
        snapshot=str(state.get("snapshot", "")),
        browser_tools=state.get("browser_tools", ()),
    )
    result = run.result
    return {
        "status": result.status,
        "reason": result.reason,
        "model_id": run.model_id,
    }
