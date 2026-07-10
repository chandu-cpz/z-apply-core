from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from nim_router import NimRouter
from nim_router.errors import NimRouterError

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.agents.prompts import load_prompt
from z_apply_core.agents.result import OrchestratorRun
from z_apply_core.agents.router_middleware import NimRouterMiddleware
from z_apply_core.agents.specialists import build_specialists
from z_apply_core.log_labels import node_info
from z_apply_core.stream_events import FrameworkEventSink

logger = logging.getLogger(__name__)

CORE_ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS_VIRTUAL_ROOT = "/.z-apply/browser-artifacts"
CANDIDATE_CONTEXT_VIRTUAL_PATH = "/chandrakanth_v_resume.md"
DEEPAGENT_FILESYSTEM_PERMISSIONS = [
    FilesystemPermission(
        operations=["read"],
        paths=[ARTIFACTS_VIRTUAL_ROOT, f"{ARTIFACTS_VIRTUAL_ROOT}/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=[CANDIDATE_CONTEXT_VIRTUAL_PATH],
        mode="allow",
    ),
    FilesystemPermission(operations=["read"], paths=["/**"], mode="deny"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


async def run_orchestrator(
    *,
    job_url: str,
    task: str,
    snapshot: str,
    browser_tools: Sequence[BaseTool],
    config: RunnableConfig,
    human_tools: Sequence[BaseTool] = (),
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
    node_info(logger, "orchestrator", "initial model: %s", model_id)

    agent = create_deep_agent(
        model=selection.llm,
        tools=list(human_tools),
        system_prompt=load_prompt("orchestrator.md"),
        middleware=[
            ModelRetryMiddleware(max_retries=3, on_failure="error"),
            NimRouterMiddleware(router, role="orchestrator"),
        ],
        subagents=await build_specialists(router, browser_tools),
        backend=FilesystemBackend(root_dir=CORE_ROOT, virtual_mode=True),
        permissions=DEEPAGENT_FILESYSTEM_PERMISSIONS,
    )

    run_config = cast(RunnableConfig, config.copy() if config else {})

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
    summary = _validated_summary(
        _summary_from_output(stream_result.output),
        _tool_trace_from_output(stream_result.output),
    )
    return OrchestratorRun(summary=summary, model_id=model_id)


def _task_prompt(*, job_url: str, task: str, snapshot: str) -> str:
    return f"""Run the requested orchestration task for this job application URL.

Job URL:
{job_url}

The runtime has already opened the browser to the job URL before this task.
Use the current browser state. Do not ask specialists to reload or navigate to
the job URL again.

Requested task:
{task}

Current browser snapshot:
{snapshot}

Delegate to BrowserSpecialist if browser evidence is needed.

Completion criteria for this run:

- Do not finish after only navigating to the form.
- Do not finish after only reporting that the resume upload is the next step.
- Before final summary, you must either attempt the resume upload or report a
  concrete blocker that prevents resume upload.
- If resume upload succeeds or is not available, continue with field mapping and
  bounded fill attempts until blocked, missing human data, or no safe remaining
  fields.
- A Verifier check must be an actual tool call after each BrowserSpecialist
  browser-changing action. If you have not received a Verifier tool result, you
  are not ready to summarize.

When finished, summarize what was actually completed, what remains, and why the
run stopped. Do not describe an intended next step as if it were completed.
"""


def _summary_from_output(output: dict[str, Any]) -> str:
    messages = output.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        content = _message_text(getattr(last, "content", ""))
        if content:
            return content[:1000]
    return "Orchestrator completed without a final message."


def _validated_summary(summary: str, tool_trace: list[dict[str, Any]]) -> str:
    issues = _trace_issues(summary, tool_trace)
    if not issues:
        return summary
    return "not_verified: " + " ".join(issues)


def _trace_issues(summary: str, tool_trace: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    issues.extend(_browser_verification_issues(tool_trace))

    if _claims_resume_upload(summary) and not _has_tool_call(tool_trace, "browser_file_upload"):
        issues.append("Final summary claimed resume upload without a browser_file_upload call.")

    if _claims_field_mapping(summary) and not _has_subagent_task(tool_trace, "FieldMapper"):
        issues.append("Final summary claimed field mapping without a FieldMapper task call.")

    if _claims_human_question(summary) and not _has_tool_call(tool_trace, "ask_human"):
        issues.append("Final summary asked for human data without an ask_human tool call.")

    return issues


def _browser_verification_issues(tool_trace: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    pending_browser_action = ""
    for call in tool_trace:
        if _is_verifier_task(call):
            pending_browser_action = ""
            continue
        if _is_browser_changing_tool(call):
            tool_name = str(call.get("tool_name", "browser action"))
            if pending_browser_action:
                issues.append(
                    "BrowserSpecialist performed another browser-changing action "
                    f"({tool_name}) before Verifier checked {pending_browser_action}."
                )
            pending_browser_action = tool_name

    if pending_browser_action:
        issues.append(
            "BrowserSpecialist performed a browser-changing action "
            f"({pending_browser_action}) without a following Verifier task."
        )
    return issues


def _claims_resume_upload(summary: str) -> bool:
    text = summary.lower()
    upload_claims = (
        "resume file has been successfully uploaded",
        "resume has been successfully uploaded",
        "successfully uploaded",
        "was successfully uploaded",
        "uploaded your resume",
    )
    return ("resume" in text or "chandrakanth-v-resume.pdf" in text) and any(
        claim in text for claim in upload_claims
    )


def _claims_field_mapping(summary: str) -> bool:
    text = summary.lower()
    return "field mapping result" in text or "map the currently visible" in text


def _claims_human_question(summary: str) -> bool:
    text = summary.lower()
    human_prompt_markers = (
        "could you provide",
        "please provide",
        "once you supply",
        "we need the candidate",
    )
    return any(marker in text for marker in human_prompt_markers)


def _has_tool_call(tool_trace: list[dict[str, Any]], tool_name: str) -> bool:
    return any(call.get("tool_name") == tool_name for call in tool_trace)


def _has_subagent_task(tool_trace: list[dict[str, Any]], subagent_type: str) -> bool:
    return any(_task_subagent_type(call) == subagent_type for call in tool_trace)


def _is_verifier_task(call: dict[str, Any]) -> bool:
    return _task_subagent_type(call) == "Verifier"


def _task_subagent_type(call: dict[str, Any]) -> str:
    if call.get("source") != "orchestrator" or call.get("tool_name") != "task":
        return ""
    tool_input = call.get("input")
    if isinstance(tool_input, dict):
        subagent_type = tool_input.get("subagent_type")
        return subagent_type if isinstance(subagent_type, str) else ""
    return ""


def _is_browser_changing_tool(call: dict[str, Any]) -> bool:
    return call.get("source") == "BrowserSpecialist" and call.get("tool_name") in {
        "browser_click",
        "browser_type",
        "browser_fill_form",
        "browser_select_option",
        "browser_file_upload",
        "browser_handle_dialog",
    }


def _tool_trace_from_output(output: dict[str, Any]) -> list[dict[str, Any]]:
    trace = output.get("_z_apply_tool_trace")
    if not isinstance(trace, list):
        return []
    return [item for item in trace if isinstance(item, dict)]


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
