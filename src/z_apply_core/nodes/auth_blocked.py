from __future__ import annotations

from langchain_core.runnables.config import RunnableConfig

from z_apply_core.state import RunState


async def auth_blocked(state: RunState, config: RunnableConfig) -> dict[str, str]:
    """Terminal node: hard-block the run when the auth phase failed or was blocked.

    The auth verdict was stored by ``authenticate_default_account``; this node
    converts a hard auth failure into a clean ``blocked`` terminal state so the
    orchestrator never runs against an unauthenticated, unresolvable session.
    ``run_status`` and ``orchestrator_summary`` are the fields the integration
    layer reads for the terminal outcome and summary.
    """
    del config
    auth_status = str(state.get("auth_status", "failed"))
    auth_summary = str(state.get("auth_summary", "")).strip()
    summary = f"Application blocked before start: authentication {auth_status}."
    if auth_summary:
        summary = f"{summary} {auth_summary}"
    return {
        "run_status": "blocked",
        "orchestrator_summary": summary,
        "model_id": str(state.get("auth_model_id", "")),
        "snapshot": str(state.get("snapshot", "")),
    }
