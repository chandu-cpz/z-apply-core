from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from z_apply_core.browser_observation import BrowserCapabilities
from z_apply_core.browser_session import BrowserSession

CAPABILITY_CONTEXT_SOURCE = "browser_capability_controller"
_READ_BROWSER_TOOLS = frozenset({"browser_observe", "browser_snapshot", "browser_find"})
_NON_FORM_BROWSER_TOOLS = _READ_BROWSER_TOOLS | frozenset(
    {
        "browser_navigate",
        "browser_click",
        "browser_tabs",
        "browser_wait_for",
        "browser_handle_dialog",
    }
)
_ALWAYS_AVAILABLE = frozenset(
    {
        "task",
        "ask_human",
        "application_blocked",
        "browser_wait_for",
    }
)
_ORCHESTRATOR_CONTROL_TOOLS = _ALWAYS_AVAILABLE | frozenset(
    {
        "request_submit_approval",
        "application_submitted",
    }
)


class CapabilityContextMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Narrow model-visible actions using trusted compositional browser facts."""

    def __init__(self, browser: BrowserSession | None) -> None:
        super().__init__()
        self._browser = browser

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        browser = self._browser
        if browser is None:
            return await handler(request)
        capabilities: BrowserCapabilities | None
        try:
            capabilities = await browser.inspect_capabilities()
        except Exception:
            capabilities = None

        tools = self._filter_tools(request.tools, capabilities)
        observation = browser.current_observation
        revision = observation.revision if observation is not None else 0
        available = ", ".join(_tool_name(tool) for tool in tools)
        current_evidence = (
            "\nCURRENT BROWSER EVIDENCE\n" + observation.compact_render()
            if observation is not None
            else ""
        )
        context = HumanMessage(
            name=CAPABILITY_CONTEXT_SOURCE,
            additional_kwargs={"lc_source": CAPABILITY_CONTEXT_SOURCE},
            content=(
                "CURRENT BROWSER ACTION CONTEXT\n"
                f"browser_revision={revision}\n"
                f"{capabilities.render() if capabilities is not None else 'capability_inspection=unavailable'}\n"
                f"available_tools={available or '(none)'}\n"
                "Use current browser evidence and choose one legal native action. "
                "These are compositional structural facts, not a workflow phase."
                f"{current_evidence}"
            ),
        )
        return await handler(
            request.override(
                messages=[*request.messages, context],
                tools=tools,
            )
        )

    @staticmethod
    def _filter_tools(
        tools: list[BaseTool | dict[str, Any]],
        capabilities: BrowserCapabilities | None,
    ) -> list[BaseTool | dict[str, Any]]:
        tools = [
            tool
            for tool in tools
            if _tool_name(tool).startswith("browser_")
            or _tool_name(tool) in _ORCHESTRATOR_CONTROL_TOOLS
        ]
        if capabilities is None:
            safe = _READ_BROWSER_TOOLS | frozenset(
                {"browser_wait_for", "application_blocked"}
            )
            return [tool for tool in tools if _tool_name(tool) in safe]
        if capabilities.auth_gate_visible:
            allowed = _READ_BROWSER_TOOLS | _ALWAYS_AVAILABLE
            return [tool for tool in tools if _tool_name(tool) in allowed]
        if not capabilities.editable_controls_visible:
            allowed = _NON_FORM_BROWSER_TOOLS | frozenset(
                {"application_blocked", "application_submitted"}
            )
            if capabilities.visual_only_surface_visible:
                allowed |= frozenset({"task"})
            return [tool for tool in tools if _tool_name(tool) in allowed]
        if capabilities.required_file_upload_pending:
            return [
                tool
                for tool in tools
                if _tool_name(tool)
                not in {"request_submit_approval", "application_submitted", "task"}
            ]
        candidate_resolution_needed = bool(
            capabilities.unresolved_required_controls or capabilities.invalid_controls
        )
        if not candidate_resolution_needed and not capabilities.visual_only_surface_visible:
            return [tool for tool in tools if _tool_name(tool) != "task"]
        return tools


def _tool_name(tool: BaseTool | dict[str, Any]) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return str(tool.get("name", ""))
