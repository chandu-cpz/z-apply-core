from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import BaseTool
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.result import OrchestratorResult, OrchestratorRun
from z_apply_core.agents.specialists import build_specialists
from z_apply_core.log_labels import node_info
from z_apply_core.stream_events import consume_v3_events

logger = logging.getLogger(__name__)


async def run_orchestrator(
    *,
    job_url: str,
    task: str,
    snapshot: str,
    browser_tools: Sequence[BaseTool],
) -> OrchestratorRun:
    try:
        selection = await NimRouter().select(tools=True, structured=True, priority="balanced")
    except (NimRouterError, ImportError, ValueError) as exc:
        result = OrchestratorResult(status="failed", reason=f"Model selection failed: {exc}")
        return OrchestratorRun(result=result, model_id="")

    model_id = selection.info.id
    node_info(logger, "orchestrator", "selected model: %s", model_id)

    agent = create_deep_agent(
        model=selection.llm,
        tools=[],
        system_prompt=load_prompt("orchestrator.md"),
        middleware=[ModelRetryMiddleware(max_retries=3, on_failure="error")],
        subagents=build_specialists(browser_tools),
        response_format=ToolStrategy(schema=OrchestratorResult),
    )
    stream = agent.astream_events(
        {
            "messages": [
                {
                    "role": "user",
                    "content": _task_prompt(job_url=job_url, task=task, snapshot=snapshot),
                }
            ]
        },
        config={"callbacks": [selection.callback]},
        version="v3",
    )
    stream_result = await consume_v3_events(stream)
    structured = stream_result.output.get("structured_response")
    if isinstance(structured, OrchestratorResult):
        return OrchestratorRun(result=structured, model_id=model_id)
    if isinstance(structured, dict):
        return OrchestratorRun(
            result=OrchestratorResult.model_validate(structured),
            model_id=model_id,
        )
    return OrchestratorRun(result=_fallback_result(stream_result.output), model_id=model_id)


def _task_prompt(*, job_url: str, task: str, snapshot: str) -> str:
    return f"""Run the requested orchestration task for this job application URL.

Job URL:
{job_url}

Requested task:
{task}

Current browser snapshot:
{snapshot}

Delegate to BrowserSpecialist if browser evidence is needed.
Return the final answer using the structured response tool only.
"""


def _fallback_result(output: dict[str, Any]) -> OrchestratorResult:
    messages = output.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        content = _message_text(getattr(last, "content", ""))
        if content:
            return OrchestratorResult(
                status="failed",
                reason=(
                    "No structured orchestrator result returned. "
                    f"Last model response: {content[:300]}"
                ),
            )
    return OrchestratorResult(
        status="failed",
        reason="No structured orchestrator result.",
    )


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
