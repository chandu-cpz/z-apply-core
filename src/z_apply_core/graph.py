from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from nim_router import NimRouter
from nim_router.config import RouterConfig

from z_apply_core.model_policy import BANNED_MODEL_IDS_UNDER_30B
from z_apply_core.nodes import authenticate_default_account, orchestrator, setup_browser
from z_apply_core.state import RunState, initial_state
from z_apply_core.stream_events import FrameworkEventSink, V3RunResult, consume_v3_events

CORE_ROOT = Path(__file__).resolve().parents[2]
ROUTER_STATS_PATH = CORE_ROOT / ".z-apply" / "nim-router-stats.json"
MAX_EXPLORATION_INTERVAL_SECONDS = 300.0


def build_graph() -> Any:
    graph = StateGraph(cast(Any, RunState))
    graph.add_node("setup_browser", setup_browser)
    graph.add_node("authenticate_default_account", authenticate_default_account)
    graph.add_node("orchestrator", orchestrator)
    graph.add_edge(START, "setup_browser")
    graph.add_edge("setup_browser", "authenticate_default_account")
    graph.add_edge("authenticate_default_account", "orchestrator")
    graph.add_edge("orchestrator", END)
    return graph.compile()


async def run_job(
    job_url: str,
    *,
    task: str,
    live_view: bool = True,
    sink: FrameworkEventSink | None = None,
) -> tuple[RunState, V3RunResult]:
    graph = build_graph()
    runtime = None
    router_config = RouterConfig.from_env()
    router_config.excluded_models = list(
        dict.fromkeys([*router_config.excluded_models, *BANNED_MODEL_IDS_UNDER_30B])
    )
    router_config.stats_path = str(ROUTER_STATS_PATH)
    router_config.timeout_seconds = min(router_config.timeout_seconds, 20.0)
    router_config.exploration_interval_seconds = min(
        router_config.exploration_interval_seconds,
        MAX_EXPLORATION_INTERVAL_SECONDS,
    )
    router = NimRouter(config=router_config)
    try:
        stream = graph.astream_events(
            initial_state(job_url, task=task, live_view=live_view),
            config={"configurable": {"sink": sink, "nim_router": router}},
            version="v3",
        )
        result = await consume_v3_events(stream, sink=sink)
        runtime = result.output.get("runtime")
        return cast(RunState, result.output), result
    finally:
        if runtime is not None:
            await runtime.close()
