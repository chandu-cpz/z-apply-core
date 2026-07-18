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
from z_apply_core.agents.specialists.answer_writer import CandidateFieldAnswer
from z_apply_core.browser_session import BrowserSession
from z_apply_core.browser_tools import (
    BROWSER_CHANGING_TOOL_NAMES,
    normalize_browser_arguments,
)


class OrchestratorActionOrderMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Enforce cross-turn application invariants at the native action boundary."""

    def __init__(self, browser: BrowserSession | None) -> None:
        super().__init__()
        self._browser = browser
        self._candidate_answers: list[CandidateFieldAnswer] = []

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        response: ModelResponse[ResponseT] = await handler(request)
        violation = await self._violation(response)
        if violation is None:
            return response

        retry: ModelResponse[ResponseT] = await handler(
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
            self._candidate_answers.extend(_candidate_answers(result))
        elif call.get("name") in BROWSER_CHANGING_TOOL_NAMES and _tool_succeeded(result):
            for entry in _candidate_entries(call):
                index = _matching_answer_index(self._candidate_answers, entry)
                if index is not None:
                    self._candidate_answers.pop(index)
        return result

    async def _violation(self, response: ModelResponse[ResponseT]) -> str | None:
        calls = _tool_calls(response)
        if not calls:
            return None
        entries = [entry for call in calls for entry in _candidate_entries(call)]
        if entries and not _entries_match_answers(entries, self._candidate_answers):
            return (
                "CANDIDATE EVIDENCE ERROR: each candidate mutation must exactly match "
                "the target and value in a successful structured AnswerWriter result. "
                "Call resolve_candidate_field for the exact current field and ref, then "
                "apply only that returned target and value."
            )
        if self._candidate_answers and not entries:
            return (
                "ACTION ORDER ERROR: AnswerWriter results are waiting to be applied. "
                "Your next native action must apply the exact returned target and value. "
                "Do not inspect, click elsewhere, plan, or dispatch another specialist first."
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


def _candidate_entries(call: Mapping[str, Any]) -> list[tuple[str, str]]:
    name = call.get("name")
    raw_args = call.get("args")
    if not isinstance(raw_args, dict):
        return []
    args = normalize_browser_arguments(raw_args)
    if name == "browser_type":
        return _entries_from_fields([args])
    if name == "browser_select_option":
        values = args.get("values")
        if not isinstance(values, list) or len(values) != 1:
            return []
        return _entries_from_fields([{**args, "value": values[0]}])
    if name == "browser_fill_form":
        fields = args.get("fields")
        return _entries_from_fields(fields if isinstance(fields, list) else [])
    return []


def _entries_from_fields(fields: list[Any]) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        target = field.get("target")
        value = field.get("value", field.get("text"))
        if isinstance(target, str) and isinstance(value, str):
            entries.append((target, value))
    return entries


def _matching_answer_index(
    answers: list[CandidateFieldAnswer], entry: tuple[str, str]
) -> int | None:
    target, value = entry
    return next(
        (
            index
            for index, answer in enumerate(answers)
            if answer.target == target and answer.value == value
        ),
        None,
    )


def _entries_match_answers(
    entries: list[tuple[str, str]], answers: list[CandidateFieldAnswer]
) -> bool:
    remaining = list(answers)
    for entry in entries:
        index = _matching_answer_index(remaining, entry)
        if index is None:
            return False
        remaining.pop(index)
    return True


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


def _candidate_answers(result: ToolMessage | Command[Any]) -> list[CandidateFieldAnswer]:
    if not _tool_succeeded(result):
        return []
    answers: list[CandidateFieldAnswer] = []
    for message in _tool_messages(result):
        try:
            answers.append(CandidateFieldAnswer.model_validate_json(message.text))
        except ValueError:
            continue
    return answers
