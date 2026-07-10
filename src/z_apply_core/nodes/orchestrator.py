from __future__ import annotations

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from nim_router import NimRouter

from z_apply_core.agents.orchestrator import run_orchestrator
from z_apply_core.human.tools import make_human_tools
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkEventSink


async def orchestrator(state: RunState, config: RunnableConfig) -> dict[str, str]:
    sink = _sink_from_config(config)
    router = _router_from_config(config)
    run = await run_orchestrator(
        job_url=str(state["job_url"]),
        task=str(state["task"]),
        snapshot=str(state.get("snapshot", "")),
        browser_tools=state.get("browser_tools", ()),
        config=config,
        human_tools=_human_tools(state),
        sink=sink,
        router=router,
    )
    snapshot = await _fresh_snapshot(state)
    return {
        "orchestrator_summary": run.summary,
        "model_id": run.model_id,
        "snapshot": snapshot,
    }


def _sink_from_config(config: RunnableConfig) -> FrameworkEventSink | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    sink = configurable.get("sink")
    if hasattr(sink, "accept"):
        return sink
    return None


def _router_from_config(config: RunnableConfig) -> NimRouter:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise ValueError(
            "Run config is missing 'configurable'; cannot locate the shared NimRouter."
        )
    router = configurable.get("nim_router")
    if not isinstance(router, NimRouter):
        raise ValueError(
            "configurable['nim_router'] is missing or not a NimRouter instance."
        )
    return router


async def _fresh_snapshot(state: RunState) -> str:
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime):
        return str(state.get("snapshot", ""))
    snapshot = await runtime.browser.tools.call("browser_snapshot")
    return snapshot or str(state.get("snapshot", ""))


def _human_tools(state: RunState) -> list[BaseTool]:
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime) or runtime.human_channel is None:
        return []
    return make_human_tools(runtime.human_channel)
