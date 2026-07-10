from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

RESUME_PATH = ".z-apply/input/Chandrakanth-V-Resume.pdf"


class BrowserOperationOutcome(BaseModel):
    """Facts captured by the deterministic browser-operation executor."""

    operation: Literal["snapshot", "click", "upload_resume"]
    status: Literal["completed", "failed"]
    steps: list[dict[str, object]]
    snapshot: str
    error: str | None = None


class BrowserOperationInput(BaseModel):
    operation: Literal["snapshot", "click", "upload_resume"] = Field(
        description="The browser operation to execute."
    )
    target: str | None = Field(
        default=None,
        description="Fresh accessibility-snapshot ref required for click and upload_resume.",
    )
    element: str | None = Field(
        default=None,
        description="Optional human-readable label for trace output.",
    )


def build_browser_operation_tool(browser_tools: Sequence[BaseTool]) -> BaseTool:
    """Build the only browser mutation entrypoint exposed to the orchestrator.

    The model chooses a typed operation and a snapshot ref. This executor owns
    the actual browser calls and returns the browser evidence produced by them.
    It never derives completion from model prose.
    """
    tools = {tool.name: tool for tool in browser_tools}

    async def execute(
        operation: Literal["snapshot", "click", "upload_resume"],
        target: str | None = None,
        element: str | None = None,
    ) -> str:
        steps: list[dict[str, object]] = []
        try:
            if operation == "snapshot":
                snapshot = await _call(tools, "browser_snapshot", {}, steps)
            elif operation == "click":
                args = _target_args(target, element)
                await _call(tools, "browser_click", args, steps)
                snapshot = await _call(tools, "browser_snapshot", {}, steps)
            else:
                args = _target_args(target, element)
                await _call(tools, "browser_click", args, steps)
                await _call(tools, "browser_file_upload", {"paths": [RESUME_PATH]}, steps)
                snapshot = await _call(tools, "browser_snapshot", {}, steps)
        except Exception as exc:  # noqa: BLE001 - returned as the typed tool result
            return BrowserOperationOutcome(
                operation=operation,
                status="failed",
                steps=steps,
                snapshot="",
                error=str(exc),
            ).model_dump_json()

        return BrowserOperationOutcome(
            operation=operation,
            status="completed",
            steps=steps,
            snapshot=snapshot,
        ).model_dump_json()

    return StructuredTool.from_function(
        coroutine=execute,
        name="execute_browser_operation",
        description=(
            "Execute one typed browser operation and return its actual tool outputs plus a "
            "fresh accessibility snapshot. For upload_resume, target must be the primary "
            "resume/CV control; the executor clicks it then uploads the configured resume."
        ),
        args_schema=BrowserOperationInput,
        infer_schema=False,
    )


def _target_args(target: str | None, element: str | None) -> dict[str, str]:
    if not target:
        raise ValueError("A fresh browser snapshot ref is required for this operation.")
    args = {"target": target}
    if element:
        args["element"] = element
    return args


async def _call(
    tools: dict[str, BaseTool],
    name: str,
    arguments: Mapping[str, object],
    steps: list[dict[str, object]],
) -> str:
    tool = tools.get(name)
    if tool is None:
        raise ValueError(f"Browser operation requires unavailable tool {name!r}.")
    argument_dict = dict(arguments)
    output = str(await tool.ainvoke(argument_dict))
    steps.append({"tool": name, "arguments": argument_dict, "output": output})
    return output
