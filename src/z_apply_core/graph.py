from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from z_apply_core.nodes import setup_browser
from z_apply_core.state import RunState
from z_apply_core.stream_events import FrameworkEventSink, V3RunResult, consume_v3_events


def build_graph() -> CompiledStateGraph[RunState, None, RunState, RunState]:
    graph = StateGraph(RunState)
    graph.add_node("setup_browser", setup_browser)
    graph.add_edge(START, "setup_browser")
    graph.add_edge("setup_browser", END)
    return graph.compile()


async def run_job(
    job_url: str,
    *,
    live_view: bool = True,
    sink: FrameworkEventSink | None = None,
) -> tuple[str, V3RunResult]:
    graph = build_graph()
    runtime = None
    try:
        stream = graph.astream_events(
            RunState(job_url=job_url, live_view=live_view),
            version="v3",
        )
        result = await consume_v3_events(stream, sink=sink)
        runtime = result.output.get("runtime")
        return str(result.output.get("snapshot", "")), result
    finally:
        if runtime is not None:
            await runtime.close()
