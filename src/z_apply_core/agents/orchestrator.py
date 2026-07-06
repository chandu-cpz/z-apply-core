from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.result import OrchestratorRun
from z_apply_core.agents.specialists import build_specialists
from z_apply_core.log_labels import node_info
from z_apply_core.stream_events import FrameworkEventSink

logger = logging.getLogger(__name__)


async def run_orchestrator(
    *,
    job_url: str,
    task: str,
    snapshot: str,
    browser_tools: Sequence[BaseTool],
    config: RunnableConfig,
    sink: FrameworkEventSink | None = None,
) -> OrchestratorRun:
    try:
        selection = await NimRouter().select(tools=True, structured=True, priority="balanced")
    except (NimRouterError, ImportError, ValueError) as exc:
        return OrchestratorRun(summary=f"Model selection failed: {exc}", model_id="")

    model_id = selection.info.id
    node_info(logger, "orchestrator", "selected model: %s", model_id)

    agent = create_deep_agent(
        model=selection.llm,
        tools=[],
        system_prompt=load_prompt("orchestrator.md"),
        middleware=[ModelRetryMiddleware(max_retries=3, on_failure="error")],
        subagents=build_specialists(browser_tools),
    )

    run_config = cast(RunnableConfig, config.copy() if config else {})
    callbacks = run_config.get("callbacks")
    if isinstance(callbacks, list):
        run_config["callbacks"] = callbacks + [selection.callback]
    else:
        run_config["callbacks"] = [selection.callback]

    stream = agent.astream_events(
        {
            "messages": [
                {
                    "role": "user",
                    "content": _task_prompt(job_url=job_url, task=task, snapshot=snapshot),
                }
            ]
        },
        config=run_config,
        version="v3",
    )
    stream_result = await consume_deepagent_stream(stream, sink=sink)
    return OrchestratorRun(summary=_summary_from_output(stream_result.output), model_id=model_id)


def _task_prompt(*, job_url: str, task: str, snapshot: str) -> str:
    return f"""Run the requested orchestration task for this job application URL.

Job URL:
{job_url}

Requested task:
{task}

Current browser snapshot:
{snapshot}

Delegate to BrowserSpecialist if browser evidence is needed.
When finished, briefly summarize what was done and what page/state the browser is on.
"""


def _summary_from_output(output: dict[str, Any]) -> str:
    messages = output.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        content = _message_text(getattr(last, "content", ""))
        if content:
            return content[:1000]
    return "Orchestrator completed without a final message."


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
