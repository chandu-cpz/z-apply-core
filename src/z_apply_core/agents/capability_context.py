from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool

from z_apply_core.browser_observation import BrowserCapabilities
from z_apply_core.browser_session import BrowserSession
from z_apply_core.context.evidence_store import EvidenceStore, render_bounded
from z_apply_core.context.run_context import RunContext
from z_apply_core.memory.platform_playbooks import PlatformPlaybooks

logger = logging.getLogger(__name__)

__all__ = ["CapabilityContextMiddleware"]

CAPABILITY_CONTEXT_SOURCE = "browser_capability_controller"
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
        "application_submitted",
        "remember_platform_lesson",
        "lookup_candidate_memory",
    }
)


class CapabilityContextMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Narrow model-visible actions using trusted compositional browser facts."""

    def __init__(
        self,
        browser: BrowserSession | None,
        *,
        platform_playbooks: PlatformPlaybooks | None = None,
        job_url: str = "",
        run_context: RunContext | None = None,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        super().__init__()
        self._browser = browser
        self._platform_playbooks = platform_playbooks
        self._job_url = job_url
        self._run_context = run_context
        self._evidence_store = evidence_store
        self._last_injected_revision: int | None = None
        self._last_playbook_text: str | None = None

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        browser = self._browser
        if browser is None:
            return await handler(request)
        capabilities: BrowserCapabilities | None
        try:
            capabilities = await browser.inspect_capabilities()
        except Exception:
            capabilities = None

        pending_upload_target = browser.pending_atomic_upload_target
        tools = self._filter_tools(
            request.tools,
            capabilities,
            atomic_upload_pending=bool(pending_upload_target),
        )
        observation = browser.current_observation
        revision = observation.revision if observation is not None else None
        available = ", ".join(_tool_name(tool) for tool in tools)
        last_carries_evidence = _last_tool_message_carries_revision(request.messages, revision)
        skip_evidence = (
            revision is not None
            and self._last_injected_revision == revision
            and last_carries_evidence
        )
        if skip_evidence:
            if self._evidence_store is not None and observation is not None:
                self._evidence_store.save(observation)
            current_evidence = (
                "\nCURRENT BROWSER EVIDENCE\n"
                f"No new browser evidence has been recorded since revision {revision}. "
                "The latest tool result already carries the current post-action "
                "evidence. Call browser_observe only when you need a different view.\n"
            )
        else:
            self._last_injected_revision = revision
            if observation is not None and self._evidence_store is not None:
                current_evidence = render_bounded(observation, self._evidence_store)
            else:
                current_evidence = (
                    "\nCURRENT BROWSER EVIDENCE\n" + observation.compact_render()
                    if observation is not None
                    else ""
                )
        upload_context = (
            "pending_atomic_upload_target="
            f"{pending_upload_target}\n"
            "The last click activated a native file chooser. Your next action must "
            "call browser_click_upload with this exact target and the configured "
            "resume path. Do not observe or click again.\n"
            if pending_upload_target
            else ""
        )
        if capabilities is not None and capabilities.required_file_upload_pending:
            upload_context += (
                "required_file_upload_pending=true\n"
                "A REQUIRED file upload is still empty and the configured "
                "resume must be attached to it. Call browser_click_upload now "
                "with any ref from the resume/upload section of the current "
                "browser evidence, or with the upload control's label; the "
                "runtime resolves the file control deterministically and "
                "attaches the configured resume path.\n"
            )
        elif capabilities is not None and capabilities.empty_file_upload_present:
            upload_context += (
                "optional_empty_upload_present=true\n"
                "An OPTIONAL file-upload control is empty. It is not work: "
                "the required resume upload is either already attached or not "
                "required on this form. Ignore this control unless "
                "required_file_upload_pending becomes true.\n"
            )
        platform_context = ""
        if self._platform_playbooks is not None and self._job_url:
            playbook_text = self._platform_playbooks.read_for_url(self._job_url)
            if playbook_text != self._last_playbook_text:
                self._last_playbook_text = playbook_text
                platform_context = (
                    "\nCURRENT APPLICABLE PLATFORM PLAYBOOK\n"
                    f"{playbook_text}\n"
                    "END CURRENT APPLICABLE PLATFORM PLAYBOOK\n"
                )
        context = HumanMessage(
            name=CAPABILITY_CONTEXT_SOURCE,
            additional_kwargs={"lc_source": CAPABILITY_CONTEXT_SOURCE},
            content=(
                "CURRENT BROWSER ACTION CONTEXT\n"
                f"browser_revision={revision}\n"
                f"{capabilities.render() if capabilities is not None else 'capability_inspection=unavailable'}\n"
                f"available_tools={available or '(none)'}\n"
                f"{upload_context}"
                "Use current browser evidence and choose one legal native action. "
                "These are compositional structural facts, not a workflow phase."
                f"{platform_context}"
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
        *,
        atomic_upload_pending: bool = False,
    ) -> list[BaseTool | dict[str, Any]]:
        tools = [
            tool
            for tool in tools
            if _tool_name(tool).startswith("browser_")
            or _tool_name(tool) in _ORCHESTRATOR_CONTROL_TOOLS
        ]
        if atomic_upload_pending:
            return [tool for tool in tools if _tool_name(tool) == "browser_click_upload"]
        return tools


def _tool_name(tool: BaseTool | dict[str, Any]) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return str(tool.get("name", ""))


def _last_tool_message_carries_revision(
    messages: list[Any],
    revision: int | None,
) -> bool:
    """Whether the most recent tool result embeds the current browser evidence.

    Evidence-carrying tool results carry the typed ``browser_revision`` they
    observed in their ``additional_kwargs`` (receipts, bounded waits, snapshots,
    and ``browser_observe``). Error results never carry evidence. A non-evidence
    result (task, ask_human, denial) forces a fresh injection.
    """
    if revision is None:
        return False
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            if message.status == "error":
                return False
            return message.additional_kwargs.get("browser_revision") == revision
        if isinstance(message, AIMessage):
            return False
    return False
