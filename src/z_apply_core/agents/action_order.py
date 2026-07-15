from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import AIMessage, HumanMessage

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
        self._answers_waiting_to_be_applied = False

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT]:
        response: ModelResponse[ResponseT] = await handler(request)
        violation = await self._violation(response)
        if violation is None:
            self._record_accepted_action(response)
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
        self._record_accepted_action(retry)
        return retry

    async def _violation(self, response: ModelResponse[ResponseT]) -> str | None:
        calls = _tool_calls(response)
        if not calls:
            return None
        if self._answers_waiting_to_be_applied and not any(
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

    def _record_accepted_action(self, response: ModelResponse[ResponseT]) -> None:
        calls = _tool_calls(response)
        if any(call.get("name") in BROWSER_CHANGING_TOOL_NAMES for call in calls):
            self._answers_waiting_to_be_applied = False
        if any(_is_answer_writer(call) for call in calls):
            self._answers_waiting_to_be_applied = True


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
