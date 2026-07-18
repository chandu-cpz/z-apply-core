from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.browser_session import BrowserSession
from z_apply_core.browser_tools import BROWSER_CHANGING_TOOL_NAMES


class OrchestratorActionOrderMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Enforce cross-turn application invariants at the native action boundary."""

    def __init__(self, browser: BrowserSession | None) -> None:
        super().__init__()
        self._browser = browser
        self._candidate_answers_available = 0

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        response: ModelResponse[ResponseT] = await handler(request)
        violation = await self._violation(response)
        if violation is None:
            return response

        retry = await handler(
            request.override(
                messages=[
                    *request.messages,
                    HumanMessage(content=violation, name="action_order_controller"),
                ]
            )
        )
        retry_violation = await self._violation(retry)
        if retry_violation is not None:
            raise ToolProtocolViolation(
                "tool_protocol_failure: model repeated an action-order violation "
                "after controller correction"
            )
        return retry

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Any,
    ) -> ToolMessage | Command[Any]:
        """Advance ordering state only from completed, usable tool results."""
        result: ToolMessage | Command[Any] = await handler(request)
        call = request.tool_call
        if _is_answer_writer(call):
            if _has_nonempty_success(result):
                self._candidate_answers_available += 1
        elif call.get("name") == "ask_human" and _has_nonempty_success(result):
            self._candidate_answers_available += 1
        elif call.get("name") in BROWSER_CHANGING_TOOL_NAMES and _tool_succeeded(result):
            consumed = _text_entry_count(call)
            if consumed:
                self._candidate_answers_available = max(
                    0, self._candidate_answers_available - consumed
                )
            else:
                self._candidate_answers_available = 0
        return result

    async def _violation(self, response: ModelResponse[ResponseT]) -> str | None:
        calls = _tool_calls(response)
        if not calls:
            return None
        text_entries = sum(_text_entry_count(call) for call in calls)
        if text_entries > self._candidate_answers_available:
            return (
                "CANDIDATE EVIDENCE ERROR: browser text entry is allowed only after one "
                "successful AnswerWriter result per field (or one completed human "
                "challenge answer). Delegate one AnswerWriter task for each exact "
                "required candidate field, ignore empty optional fields, then apply only "
                "the returned values."
            )
        if self._candidate_answers_available and not any(
            call.get("name") in BROWSER_CHANGING_TOOL_NAMES for call in calls
        ):
            return (
                "ACTION ORDER ERROR: AnswerWriter results are waiting to be applied. "
                "Your next native action must be a browser mutation that consumes those "
                "answers at the known field refs. Do not inspect, search, plan, or dispatch "
                "another specialist first."
            )
        if self._browser is not None and any(_is_answer_writer(call) for call in calls):
            try:
                capabilities = await self._browser.inspect_capabilities()
            except Exception:
                capabilities = None
            if capabilities is not None and capabilities.auth_gate_visible:
                return (
                    "ACTION ORDER ERROR: the live page structurally contains a password "
                    "or one-time-code gate. Delegate AuthenticationSpecialist and do not "
                    "ask AnswerWriter for authentication data."
                )
            try:
                upload_pending = await self._browser.required_file_upload_pending()
            except Exception:  # Browser inspection failure must remain recoverable by the agent.
                upload_pending = False
            if upload_pending:
                return (
                    "ACTION ORDER ERROR: the live form has an unattached required file "
                    "input. Upload the configured resume through browser_click_upload "
                    "before resolving individual candidate fields."
                )
        return None

def _tool_calls(response: ModelResponse[Any]) -> list[Mapping[str, Any]]:
    calls: list[Mapping[str, Any]] = []
    for message in response.result:
        if isinstance(message, AIMessage):
            calls.extend(message.tool_calls)
    return calls


def _is_answer_writer(call: Mapping[str, Any]) -> bool:
    if call.get("name") != "task":
        return False
    args = call.get("args")
    return isinstance(args, dict) and args.get("subagent_type") == "AnswerWriter"


def _text_entry_count(call: Mapping[str, Any]) -> int:
    name = call.get("name")
    if name == "browser_type":
        return 1
    if name != "browser_fill_form":
        return 0
    args = call.get("args")
    fields = args.get("fields") if isinstance(args, dict) else None
    return len(fields) if isinstance(fields, list) and fields else 1


def _tool_messages(result: ToolMessage | Command[Any]) -> list[ToolMessage]:
    if isinstance(result, ToolMessage):
        return [result]
    update = result.update
    if not isinstance(update, dict):
        return []
    messages = update.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, ToolMessage)]


def _tool_succeeded(result: ToolMessage | Command[Any]) -> bool:
    messages = _tool_messages(result)
    return bool(messages) and all(message.status != "error" for message in messages)


def _has_nonempty_success(result: ToolMessage | Command[Any]) -> bool:
    return _tool_succeeded(result) and any(message.text.strip() for message in _tool_messages(result))
