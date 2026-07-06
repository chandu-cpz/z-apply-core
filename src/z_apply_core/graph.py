from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from z_apply_core.nodes import orchestrator, setup_browser
from z_apply_core.state import RunState, initial_state
from z_apply_core.stream_events import FrameworkEventSink, V3RunResult, consume_v3_events


def build_graph() -> Any:
    graph = StateGraph(cast(Any, RunState))
    graph.add_node("setup_browser", setup_browser)
    graph.add_node("orchestrator", orchestrator)
    graph.add_edge(START, "setup_browser")
    graph.add_edge("setup_browser", "orchestrator")
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
    try:
        stream = graph.astream_events(
            initial_state(job_url, task=task, live_view=live_view),
            version="v3",
        )
        result = await consume_v3_events(stream, sink=sink)
        runtime = result.output.get("runtime")
        return cast(RunState, result.output), result
    finally:
        if runtime is not None:
            await runtime.close()
