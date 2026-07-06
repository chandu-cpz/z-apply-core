from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import BaseTool
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.result import OrchestratorResult
from z_apply_core.agents.specialists import build_specialists
from z_apply_core.stream_events import consume_v3_events


async def run_orchestrator(
    *,
    job_url: str,
    snapshot: str,
    browser_tools: Sequence[BaseTool],
) -> OrchestratorResult:
    try:
        selection = await NimRouter().select(tools=True, structured=True, priority="balanced")
    except (NimRouterError, ImportError, ValueError) as exc:
        return OrchestratorResult(status="failed", reason=f"Model selection failed: {exc}")

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
                    "content": _task_prompt(job_url=job_url, snapshot=snapshot),
                }
            ]
        },
        config={"callbacks": [selection.callback]},
        version="v3",
    )
    result = await consume_v3_events(stream)
    structured = result.output.get("structured_response")
    if isinstance(structured, OrchestratorResult):
        return structured
    if isinstance(structured, dict):
        return OrchestratorResult.model_validate(structured)
    return _fallback_result(result.output)


def _task_prompt(*, job_url: str, snapshot: str) -> str:
    return f"""Inspect the starting state for this job application URL.

Job URL:
{job_url}

Current browser snapshot:
{snapshot}

Delegate to BrowserSpecialist if browser evidence is needed.
Do not perform application actions yet.
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
