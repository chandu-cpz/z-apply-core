from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.orchestrator import CORE_ROOT, DEEPAGENT_FILESYSTEM_PERMISSIONS
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.result import OrchestratorRun
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.specialists import build_auth_specialists
from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware
from z_apply_core.log_labels import node_info
from z_apply_core.stream_events import FrameworkEventSink

logger = logging.getLogger(__name__)


async def run_auth_orchestrator(
    *,
    snapshot: str,
    browser_tools: Sequence[BaseTool],
    human_tools: Sequence[BaseTool],
    config: RunnableConfig,
    sink: FrameworkEventSink | None = None,
    router: NimRouter | None = None,
) -> OrchestratorRun:
    if not isinstance(router, NimRouter):
        return OrchestratorRun(
            summary="Model routing failed: shared NimRouter was not provided.",
            model_id="",
        )

    try:
        selection = await router.select(tools=True, structured=True, priority="balanced")
    except (NimRouterError, ImportError, ValueError) as exc:
        return OrchestratorRun(summary=f"Model selection failed: {exc}", model_id="")

    model_id = selection.info.id
    node_info(
        logger,
        "authenticate_default_account",
        "fallback model: %s (runtime routing overrides each call)",
        model_id,
    )

    agent = create_deep_agent(
        model=selection.llm,
        tools=list(human_tools),
        system_prompt=load_prompt("auth_orchestrator.md"),
        middleware=[
            SubagentDispatchMiddleware(["BrowserSpecialist", "Verifier"]),
            ModelRetryMiddleware(max_retries=3, on_failure="error"),
            NimRouterMiddleware(router, role="auth_orchestrator"),
        ],
        subagents=await build_auth_specialists(router, browser_tools),
        backend=FilesystemBackend(root_dir=CORE_ROOT, virtual_mode=True),
        permissions=DEEPAGENT_FILESYSTEM_PERMISSIONS,
    )

    run_config = cast(RunnableConfig, config.copy() if config else {})

    stream = agent.astream_events(
        {
            "messages": [
                {
                    "role": "user",
                    "content": _task_prompt(snapshot=snapshot),
                }
            ]
        },
        config=run_config,
        version="v3",
    )
    stream_result = await consume_deepagent_stream(stream, sink=sink)
    return OrchestratorRun(summary=_summary_from_output(stream_result.output), model_id=model_id)


def _task_prompt(*, snapshot: str) -> str:
    return f"""Authenticate the default Simplify account if needed.

The runtime has already opened the browser to Simplify before this task.
Use the current browser state. Do not navigate away from Simplify.

Current browser snapshot:
{snapshot}
"""


def _summary_from_output(output: dict[str, Any]) -> str:
    messages = output.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        content = _message_text(getattr(last, "content", ""))
        if content:
            return content[:1000]
    return "Auth orchestrator completed without a final message."


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    return str(content).strip()
