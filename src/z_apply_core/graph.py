from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from z_apply_core.agents.context_inbox import ContextInbox
from z_apply_core.agents.model_provider import ModelProvider, get_provider
from z_apply_core.nodes import (
    auth_blocked,
    authenticate_default_account,
    orchestrator,
    setup_browser,
)
from z_apply_core.runtime import RunResources, RunRuntime
from z_apply_core.state import RunState, initial_state
from z_apply_core.stream_events import FrameworkEventSink, V3RunResult, consume_v3_events

# Auth verdicts that terminate the run before any application work starts.
# ``authenticated`` and ``not_verified`` proceed (the latter is ambiguous and
# the AuthenticationSpecialist can still resolve gates mid-application); a
# ``failed`` (exception) or ``blocked`` (human auth action unresolved) verdict
# hard-blocks the run.
HARD_BLOCK_AUTH_STATUSES = frozenset({"failed", "blocked"})


def _route_after_auth(state: RunState) -> str:
    """Route to the orchestrator unless the auth phase hard-failed."""
    if str(state.get("auth_status", "")) in HARD_BLOCK_AUTH_STATUSES:
        return "auth_blocked"
    return "orchestrator"


def build_graph() -> Any:
    graph = StateGraph(cast(Any, RunState))
    graph.add_node("setup_browser", setup_browser)
    graph.add_node("authenticate_default_account", authenticate_default_account)
    graph.add_node("auth_blocked", auth_blocked)
    graph.add_node("orchestrator", orchestrator)
    graph.add_edge(START, "setup_browser")
    graph.add_edge("setup_browser", "authenticate_default_account")
    graph.add_conditional_edges(
        "authenticate_default_account",
        _route_after_auth,
        {"auth_blocked": "auth_blocked", "orchestrator": "orchestrator"},
    )
    graph.add_edge("auth_blocked", END)
    graph.add_edge("orchestrator", END)
    return graph.compile()


async def run_job(
    job_url: str,
    *,
    task: str,
    live_view: bool = True,
    prompt_variant: str | None = None,
    prompt_sha: str | None = None,
    sink: FrameworkEventSink | None = None,
    provider: ModelProvider | None = None,
    provider_name: str | None = None,
    resources: RunResources | None = None,
    cleanup_resources: bool = True,
    context_inbox: ContextInbox | None = None,
    prepared_runtime: RunRuntime | None = None,
    call_ledger: Any | None = None,
) -> tuple[RunState, V3RunResult]:
    graph = build_graph()
    run_resources = resources or RunResources()
    resolved_provider = provider or get_provider(provider_name=provider_name)
    try:
        stream = graph.astream_events(
            initial_state(
                job_url,
                task=task,
                live_view=live_view,
                prompt_variant=prompt_variant,
                prompt_sha=prompt_sha,
            ),
            config={
                "configurable": {
                    "sink": sink,
                    "model_provider": resolved_provider,
                    "run_resources": run_resources,
                    "context_inbox": context_inbox,
                    "prepared_runtime": prepared_runtime,
                    "call_ledger": call_ledger,
                }
            },
            version="v3",
        )
        result = await consume_v3_events(stream, sink=sink)
        return cast(RunState, result.output), result
    finally:
        if cleanup_resources and run_resources.runtime is not None:
            await run_resources.runtime.close()
