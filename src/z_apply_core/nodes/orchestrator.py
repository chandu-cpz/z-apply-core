from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from nim_router import NimRouter

from z_apply_core.agents.orchestrator import run_orchestrator
from z_apply_core.human.tools import make_human_tools
from z_apply_core.runtime import RunRuntime
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkEventSink

CORE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RESUME_PATH = (CORE_ROOT / ".z-apply" / "input" / "Chandrakanth-V-Resume.pdf").resolve()
_log = logging.getLogger(__name__)


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
        resume_path=str(DEFAULT_RESUME_PATH),
    )
    snapshot = await _fresh_snapshot(state)
    return {
        "orchestrator_summary": run.summary,
        "model_id": run.model_id,
        "run_status": run.status,
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
    fallback = str(state.get("snapshot", ""))
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime):
        return fallback
    try:
        snapshot = await runtime.browser.tools.call("browser_snapshot")
    except Exception as exc:  # noqa: BLE001 - preserve the completed run result
        _log.warning("Final browser snapshot unavailable: %s", exc)
        return fallback
    return snapshot or fallback


def _human_tools(state: RunState) -> list[BaseTool]:
    runtime = state.get("runtime")
    if not isinstance(runtime, RunRuntime) or runtime.human_channel is None:
        return []
    return make_human_tools(runtime.human_channel)
