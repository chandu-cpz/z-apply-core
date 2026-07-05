from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from z_apply_core.nodes import setup_browser
from z_apply_core.state import RunState


def build_graph() -> CompiledStateGraph[RunState, None, RunState, RunState]:
    graph = StateGraph(RunState)
    graph.add_node("setup_browser", setup_browser)
    graph.add_edge(START, "setup_browser")
    graph.add_edge("setup_browser", END)
    return graph.compile()


async def run_job(job_url: str, *, live_view: bool = True) -> str:
    graph = build_graph()
    runtime = None
    try:
        result: Any = await graph.ainvoke(RunState(job_url=job_url, live_view=live_view))
        if isinstance(result, dict):
            runtime = result.get("runtime")
            return str(result.get("snapshot", ""))
        runtime = result.runtime
        return str(result.snapshot)
    finally:
        if runtime is not None:
            await runtime.close()
