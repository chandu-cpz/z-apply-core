from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.browser_tools import BROWSER_CHANGING_TOOL_NAMES

if TYPE_CHECKING:
    from z_apply_core.browser_session import BrowserSession

logger = logging.getLogger(__name__)

_PROGRESS_TOOL_NAMES = BROWSER_CHANGING_TOOL_NAMES | {
    "application_blocked",
    "application_submitted",
    "ask_human",
    "request_manual_auth",
    "task",
}
_REPEATABLE_READ_TOOL_NAMES = frozenset({"browser_wait_for"})
# Tools not subject to the identical-call slap: timed waits are legitimately
# repeatable, terminal/review tools are single-shot flow signals, and subagent
# delegation may genuinely be retried after an internal failure even with the
# same parameters.
_IDENTICAL_CALL_EXEMPT_TOOL_NAMES = frozenset(
    {
        "application_blocked",
        "application_submitted",
        "ask_human",
        "browser_wait_for",
        "request_manual_auth",
        "task",
    }
)


class NoProgressCircuitOpen(RuntimeError):
    """The active agent repeatedly attempted calls that cannot make progress."""


class NoProgressGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """End one agent turn after repeated denied or duplicate non-progress calls.

    The first layer refuses the exact same tool call (same name and arguments)
    before the model has a chance to repeat it inside the same browser
    revision. The model is told, in no uncertain terms, that it is repeating
    itself and must do something different -- the identical call is a
    no-progress loop, not a retry.
    """

    def __init__(
        self,
        *,
        max_identical_denials: int = 2,
        max_non_progress: int = 3,
        max_state_action_failures: int = 3,
        max_stagnant_tool_calls: int | None = None,
        max_stagnant_model_responses: int | None = None,
        window_size: int = 8,
        repetition_threshold: int = 4,
        browser: BrowserSession | None = None,
        on_no_progress: Callable[[ToolProtocolViolation], None] | None = None,
    ) -> None:
        super().__init__()
        self._max_identical_denials = max_identical_denials
        self._max_non_progress = max_non_progress
        self._max_state_action_failures = max_state_action_failures
        self._max_stagnant_tool_calls = max_stagnant_tool_calls
        self._max_stagnant_model_responses = max_stagnant_model_responses
        self._window_size = max(1, window_size)
        self._repetition_threshold = max(2, repetition_threshold)
        self._browser = browser
        self._last_denial = ""
        self._same_denials = 0
        self._non_progress = 0
        self._on_no_progress = on_no_progress
        self._last_read_signature: str | None = None
        self._browser_signature: str | None = None
        self._state_action_failures: dict[str, int] = {}
        self._blocked_state_actions: set[str] = set()
        self._identical_attempts: dict[str, int] = {}
        self._stagnant_tool_calls = 0
        self._stagnant_model_responses = 0
        self._action_window: deque[tuple[str, frozenset[str]] | None] = deque(
            maxlen=self._window_size
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """End a root turn that repeatedly chooses only bookkeeping tools."""
        result = await handler(request)
        limit = self._max_stagnant_model_responses
        if limit is None:
            return result

        tool_names = {
            str(call.get("name", ""))
            for message in result.result
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        }
        if tool_names & _PROGRESS_TOOL_NAMES:
            self._stagnant_model_responses = 0
            return result
        if not tool_names:
            return result

        self._stagnant_model_responses += 1
        logger.warning(
            "Model selected only non-progress tools against unchanged state (%s/%s): %s",
            self._stagnant_model_responses,
            limit,
            ", ".join(sorted(tool_names)),
        )
        if self._stagnant_model_responses < limit:
            return result

        failure = ToolProtocolViolation(
            "no_progress: model repeatedly selected only bookkeeping or read tools"
        )
        if self._on_no_progress is not None:
            self._on_no_progress(failure)
        self._stagnant_model_responses = 0
        raise NoProgressCircuitOpen(
            "Model repeatedly chose tools that cannot advance the application; "
            "ending this turn for persistent-goal recovery."
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        self._refresh_browser_state()
        before_browser_signature = self._browser_signature
        state_action = self._state_action_signature(request)
        read_signature = _read_signature(request)
        result: ToolMessage | Command[Any]
        attempt_no = self._identical_attempts.get(state_action, 0)
        self._identical_attempts[state_action] = attempt_no + 1
        identical_call_repeated = (
            self._browser is not None
            and self._browser_signature is not None
            and self._is_repeatable_identical_call(request, attempt_no)
        )
        # Identical call slap: if the model already issued this exact tool call
        # with the exact same arguments against the same unchanged browser
        # revision, refuse it before it executes again. Repeating an identical
        # call cannot make progress, so instead of burning another turn the
        # guard smacks the model on the head and forces it to do something
        # different. A browser revision change (real progress) resets the
        # count via the embedded signature.
        if self._browser is not None and state_action in self._blocked_state_actions:
            result = ToolMessage(
                content=(
                    "RUNTIME STATE-ACTION CIRCUIT: this exact action repeatedly failed "
                    "against the current browser revision. It is unavailable until "
                    "browser evidence changes. Choose a different action or inspect "
                    "fresh evidence."
                ),
                name=str(request.tool_call.get("name", "runtime")),
                tool_call_id=str(request.tool_call.get("id", "")),
                status="error",
            )
        elif identical_call_repeated:
            result = ToolMessage(
                content=(
                    "RUNTIME IDENTICAL-CALL SLAP: this is the SECOND time you are "
                    f"issuing this exact tool call ({str(request.tool_call.get('name', 'runtime'))}) "
                    "with the exact same parameters against the same unchanged browser "
                    "revision. THAT IS A NO-PROGRESS LOOP - you are wasting the turn "
                    "repeating yourself. STOP repeating it. Do not call this exact "
                    "tool with these exact same parameters again. Instead: inspect "
                    "fresh browser evidence, then choose a DIFFERENT action - act on "
                    "a different control, or proceed even if an open list stays open."
                ),
                name=str(request.tool_call.get("name", "runtime")),
                tool_call_id=str(request.tool_call.get("id", "")),
                status="error",
            )
        elif read_signature is not None and read_signature == self._last_read_signature:
            result = ToolMessage(
                content=(
                    "RUNTIME NO-PROGRESS: this exact read-only tool call already "
                    "succeeded against unchanged state. Reuse its result and choose "
                    "a different action."
                ),
                name=str(request.tool_call.get("name", "runtime")),
                tool_call_id=str(request.tool_call.get("id", "")),
                status="error",
            )
        else:
            result = await handler(request)

        self._refresh_browser_state()
        browser_advanced = self._browser_signature != before_browser_signature

        tool_name = str(request.tool_call.get("name", ""))
        # Subagent delegation (`task`) is productive resolution work even when
        # the browser revision does not change: per-field AnswerWriter dispatch
        # makes a resolution phase legitimately task-heavy, so those calls must
        # not feed the stagnant counter. Genuine browser-mutation churn and
        # read-heavy loops still do (reads are also caught by the read-repeat
        # denial below).
        if browser_advanced or tool_name in {
            "application_blocked",
            "application_submitted",
            "ask_human",
            "request_manual_auth",
            "task",
        }:
            self._stagnant_tool_calls = 0
        else:
            self._stagnant_tool_calls += 1
        if _tool_succeeded(result):
            if tool_name in _PROGRESS_TOOL_NAMES:
                self._last_read_signature = None
            elif read_signature is not None:
                self._last_read_signature = read_signature
        self._track_repeated_action(request.tool_call, _tool_succeeded(result))
        if (
            self._browser is not None
            and isinstance(result, ToolMessage)
            and result.status == "error"
        ):
            failures = self._state_action_failures.get(state_action, 0) + 1
            self._state_action_failures[state_action] = failures
            if failures >= self._max_state_action_failures:
                self._blocked_state_actions.add(state_action)

        if _is_non_progress(result):
            detail = str(getattr(result, "content", ""))
            self._non_progress += 1
            self._same_denials = self._same_denials + 1 if detail == self._last_denial else 1
            self._last_denial = detail
            if (
                self._same_denials >= self._max_identical_denials
                or self._non_progress >= self._max_non_progress
            ):
                failure = ToolProtocolViolation(
                    "no_progress: repeated denied or non-progress tool calls require a "
                    "different model and action"
                )
                if self._on_no_progress is not None:
                    self._on_no_progress(failure)
                self._last_denial = ""
                self._same_denials = 0
                self._non_progress = 0
                raise NoProgressCircuitOpen(
                    f"Repeated denied or non-progress tool calls ({tool_name}) ended "
                    "this agent turn; resume from fresh browser evidence with a "
                    "different action."
                )
        else:
            self._last_denial = ""
            self._same_denials = 0
            self._non_progress = 0
        if (
            self._max_stagnant_tool_calls is not None
            and self._stagnant_tool_calls >= self._max_stagnant_tool_calls
        ):
            failure = ToolProtocolViolation(
                "no_progress: tool calls repeatedly completed without changing the browser revision"
            )
            if self._on_no_progress is not None:
                self._on_no_progress(failure)
            self._stagnant_tool_calls = 0
            raise NoProgressCircuitOpen(
                f"Tool activity ({tool_name}) did not advance the browser state; "
                "ending this agent turn so the persistent goal can recover from "
                "fresh evidence."
            )
        return result

    def _refresh_browser_state(self) -> None:
        observation = self._browser.current_observation if self._browser is not None else None
        signature = observation.signature if observation is not None else None
        if signature == self._browser_signature:
            return
        self._browser_signature = signature
        self._state_action_failures.clear()
        self._blocked_state_actions.clear()
        self._identical_attempts.clear()
        self._stagnant_tool_calls = 0
        self._stagnant_model_responses = 0
        self._action_window.clear()

    def _track_repeated_action(
        self,
        tool_call: Mapping[str, Any],
        succeeded: bool,
    ) -> None:
        """End the turn when the same typed action repeats within a sliding window.

        The typed signature is the tool name plus the set of refs/fields the action
        targets, with values excluded, so refilling the same field is a repeat while
        acting on different fields is not. Every tool-call step advances the window
        so repeats spread wider than ``window_size`` steps slide out.
        """
        signature = _action_signature(tool_call)
        self._action_window.append(signature if succeeded else None)
        if (
            succeeded
            and signature is not None
            and self._action_window.count(signature) >= self._repetition_threshold
        ):
            failure = ToolProtocolViolation(
                "no_progress: the same typed browser action repeated within the "
                "recent window without advancing the application"
            )
            if self._on_no_progress is not None:
                self._on_no_progress(failure)
            self._action_window.clear()
            raise NoProgressCircuitOpen(
                f"The same typed browser action ({signature[0]}) repeated within the "
                "recent window without advancing the application; ending this agent "
                "turn for fresh-evidence recovery."
            )

    def _is_repeatable_identical_call(
        self,
        request: ToolCallRequest,
        attempt_no: int,
    ) -> bool:
        """True when this exact browser call was already attempted this revision."""
        if attempt_no < 1:
            return False
        tool_name = str(request.tool_call.get("name", ""))
        if tool_name not in BROWSER_CHANGING_TOOL_NAMES:
            return False
        return tool_name not in _IDENTICAL_CALL_EXEMPT_TOOL_NAMES

    def _state_action_signature(self, request: ToolCallRequest) -> str:
        name = str(request.tool_call.get("name", ""))
        args = request.tool_call.get("args", {})
        if name == "browser_fill_form" and isinstance(args, dict):
            # Normalize fill-form args so the identical-call slap fires on a
            # refill even when the model varies the human-readable field-name
            # case ("Email" vs "email") or field order between calls: the
            # SAME fields targeted twice is the same action, full stop.
            fields = args.get("fields")
            if isinstance(fields, list):
                normalized: list[dict[str, Any]] = []
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    normalized.append(
                        {
                            "target": field.get("target", ""),
                            "name": str(field.get("name", "")).casefold(),
                            "type": field.get("type", ""),
                            "value": field.get("value", ""),
                        }
                    )
                normalized.sort(key=lambda f: str(f.get("target", "")))
                args = {**args, "fields": normalized}
        encoded = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
        return f"{self._browser_signature or '(unknown)'}:{name}:{encoded}"


def _is_non_progress(result: ToolMessage | Command[Any]) -> bool:
    if not isinstance(result, ToolMessage):
        return False
    return result.status == "error"


def _read_signature(request: ToolCallRequest) -> str | None:
    name = str(request.tool_call.get("name", ""))
    if name in _PROGRESS_TOOL_NAMES or name in _REPEATABLE_READ_TOOL_NAMES:
        return None
    args = request.tool_call.get("args", {})
    return f"{name}:{json.dumps(args, sort_keys=True, default=str, separators=(',', ':'))}"


def _tool_succeeded(result: ToolMessage | Command[Any]) -> bool:
    return not isinstance(result, ToolMessage) or result.status != "error"


def _action_signature(tool_call: Mapping[str, Any]) -> tuple[str, frozenset[str]] | None:
    """Typed identity of a browser-changing action: tool name plus targeted refs.

    Only refs/fields the action targets are kept; values are deliberately excluded
    so refilling the same field counts as a repeat while acting on different fields
    does not. Non-browser-changing tools are not loop-tracked.
    """
    name = str(tool_call.get("name", ""))
    if name not in BROWSER_CHANGING_TOOL_NAMES:
        return None
    args = tool_call.get("args") or {}
    if not isinstance(args, Mapping):
        return None
    refs = set(_target_refs(args))
    if name == "browser_navigate":
        url = args.get("url")
        if isinstance(url, str) and url:
            refs.add(url)
    return (name, frozenset(refs))


def _target_refs(args: Mapping[str, Any]) -> frozenset[str]:
    refs: set[str] = set()
    target = args.get("target")
    if isinstance(target, str) and target:
        refs.add(target)
    fields = args.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, Mapping):
                continue
            field_ref = field.get("target")
            if isinstance(field_ref, str) and field_ref:
                refs.add(field_ref)
    return frozenset(refs)
