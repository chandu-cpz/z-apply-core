from __future__ import annotations

from langchain_core.runnables.config import RunnableConfig

from z_apply_core.agents.orchestrator import run_orchestrator
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkEventSink


async def orchestrator(state: RunState, config: RunnableConfig) -> dict[str, str]:
    sink = _sink_from_config(config)
    run = await run_orchestrator(
        job_url=str(state["job_url"]),
        task=str(state["task"]),
        snapshot=str(state.get("snapshot", "")),
        browser_tools=state.get("browser_tools", ()),
        config=config,
        sink=sink,
    )
    return {
        "orchestrator_summary": run.summary,
        "model_id": run.model_id,
    }


def _sink_from_config(config: RunnableConfig) -> FrameworkEventSink | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    sink = configurable.get("sink")
    if hasattr(sink, "accept"):
        return sink
    return None
