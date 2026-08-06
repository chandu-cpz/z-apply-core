from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from z_apply_core.agents.specialists.answer_writer import (
    CandidateFieldAnswer,
    CandidateFieldRequest,
)
from z_apply_core.browser_session import BrowserSession
from z_apply_core.text_utils import alnum_key

if TYPE_CHECKING:
    from z_apply_core.memory.applicant_memory import CandidateMemory

_MASKED_VALUE_PATTERN = re.compile(r"<secret>.*?</secret>")
_MAX_CANDIDATE_FIELD_ATTEMPTS = 3


def _is_masked_value(value: str) -> bool:
    """Return True when a value is a redaction placeholder, not a real value."""
    return _MASKED_VALUE_PATTERN.search(value) is not None


async def candidate_browser_violation(
    browser: BrowserSession | None,
    request: CandidateFieldRequest,
    target: str | None = None,
) -> str | None:
    """Return why one typed request cannot act on the current browser state."""
    if browser is None:
        return (
            "CANDIDATE DELEGATION ERROR: browser validation is unavailable. "
            "Use browser_observe after the browser session is restored."
        )
    # The hard browser_revision equality gate was removed: filling one field in a
    # parallel batch bumps the shared revision, so comparing against the request's
    # snapshot revision would reject every later field. The live value and identity
    # checks below are the authoritative stale-evidence guards instead.
    if target is None:
        target = await _live_target(browser, request)
    if target is None:
        return (
            "CANDIDATE DELEGATION ERROR: the target no longer resolves in the "
            "current browser state. Call browser_observe and use a fresh target."
        )
    try:
        control = await browser.inspect_control_state(target)
    except Exception:
        return (
            "CANDIDATE DELEGATION ERROR: the target no longer resolves in the "
            "current browser state. Call browser_observe and use a fresh target."
        )
    if control.disabled:
        return (
            "CANDIDATE DELEGATION ERROR: the target is disabled and cannot accept "
            "an answer. Inspect the current form dependency instead."
        )
    if (
        not _is_masked_value(request.current_value)
        and not _is_masked_value(control.value)
        and request.current_value != control.value
    ):
        return (
            "CANDIDATE DELEGATION ERROR: current_value does not match the live "
            "target. Discard stale evidence and observe the browser again."
        )
    return None


async def _live_target(
    browser: BrowserSession,
    request: CandidateFieldRequest,
) -> str | None:
    """Return the request ref when it still resolves, else re-resolve by label.

    SPA re-renders wipe the injected ``aria-ref`` attributes that element refs
    depend on, so a request ref can die while the control itself still exists.
    Re-resolving by accessible label acts on live identity instead of dead
    evidence.
    """
    try:
        await browser.inspect_control_state(request.target)
        return request.target
    except Exception:
        return await browser.resolve_control_ref(request.field_label)


def _identity_match(control_name: str, field_label: str) -> bool:
    """Return True when a live control name can support the requested label.

    DOM ``name`` attributes are camelCase or placeholder text while evidence
    labels are spaced and titled ("firstName" vs "First Name"), so comparison is
    insensitive to case and punctuation. An empty or non-informative control name
    passes to avoid false negatives on unnamed controls. Only a definitive
    mismatch (both sides informative with no alphanumeric overlap) fails.
    """
    normalized_name = alnum_key(control_name)
    normalized_label = alnum_key(field_label)
    if not normalized_name or not normalized_label:
        return True
    return normalized_label in normalized_name or normalized_name in normalized_label


def _normalize_label_text(text: str) -> str:
    if text.casefold().strip() == "unnamed control":
        return ""
    return "".join(character for character in text.casefold() if character.isalnum())


class CandidateFieldExecutor:
    """Apply one AnswerWriter result or return recoverable browser evidence."""

    def __init__(
        self,
        browser: BrowserSession | None,
        candidate_memory: CandidateMemory | None = None,
        on_applied: Callable[[CandidateFieldAnswer], None] | None = None,
    ) -> None:
        self._browser = browser
        self._candidate_memory = candidate_memory
        self.on_applied = on_applied
        self._consecutive_failures: dict[tuple[str, str], int] = {}

    async def apply(
        self,
        tool_request: ToolCallRequest,
        result: ToolMessage | Command[Any],
        request: CandidateFieldRequest | None,
    ) -> ToolMessage | Command[Any]:
        tool_call_id = str(tool_request.tool_call.get("id", ""))
        if request is None:
            return _error(
                result,
                tool_call_id,
                "Candidate delegation has no runtime-bound request. Re-observe the "
                "browser and call resolve_candidate_field again.",
            )
        answers = _answers(result)
        if len(answers) != 1:
            return _error(
                result,
                tool_call_id,
                "AnswerWriter did not return exactly one structured CandidateFieldAnswer. "
                "Retry the typed request or rotate the model.",
            )
        answer = answers[0]
        if answer.target != request.target or answer.field_label != request.field_label:
            return _error(
                result,
                tool_call_id,
                "AnswerWriter changed the browser-bound target or field label. Discard "
                "the answer and retry from fresh evidence.",
            )
        browser = self._browser
        if browser is None:
            return _error(
                result,
                tool_call_id,
                "Browser execution is unavailable. Retry after the session is restored.",
            )
        target = await _live_target(browser, request)
        if target is None:
            return await self._recoverable(
                result,
                tool_call_id,
                "The browser target no longer resolves and its control could not "
                "be re-found by label.",
                field_label=request.field_label,
                target=request.target,
                value=answer.value,
            )
        if await candidate_browser_violation(browser, request, target=target) is not None:
            return await self._recoverable(
                result,
                tool_call_id,
                "The browser field changed before its answer could be applied.",
                field_label=request.field_label,
                target=request.target,
                value=answer.value,
            )
        current = await browser.inspect_control_state(target)
        if not _identity_match(current.control_name, request.field_label):
            refreshed = await browser.resolve_control_ref(request.field_label)
            if refreshed is None:
                return await self._recoverable(
                    result,
                    tool_call_id,
                    "The browser target now resolves to a different control than the "
                    "requested field label.",
                    field_label=request.field_label,
                    target=request.target,
                    value=answer.value,
                )
            target = refreshed
            current = await browser.inspect_control_state(target)
        if current.has_value and not current.invalid and (
            current.value == answer.value or _is_masked_value(current.value)
        ):
            self._reset_failures(request.field_label, request.target)
            await self._remember_human_answer(request, answer)
            self._record_applied(answer)
            if _is_masked_value(current.value):
                confirmation = (
                    "The browser already contains a masked value for this field; "
                    "treat it as already filled. Do not rewrite or delegate it again."
                )
            else:
                confirmation = (
                    "The browser already contains this exact evidence-backed value. "
                    "Do not rewrite or delegate it again."
                )
            return _replace_result(
                result,
                ToolMessage(
                    content=(
                        "CANDIDATE_FIELD_CONFIRMED\n"
                        f"{answer.model_dump_json()}\n{confirmation}"
                    ),
                    name="task",
                    tool_call_id=tool_call_id,
                ),
            )
        if request.control_type in {"checkbox", "radio"} and answer.value not in {
            "true",
            "false",
        }:
            return _error(
                result,
                tool_call_id,
                "Checkbox and radio answers require an exact 'true' or 'false' value. "
                "Re-observe the concrete option target and retry.",
            )
        try:
            if request.control_type == "combobox":
                receipt = await browser.call_tool_with_inline_snapshot(
                    "browser_type",
                    {"target": target, "text": answer.value},
                )
            else:
                receipt = await browser.call_tool_with_inline_snapshot(
                    "browser_fill_form",
                    {
                        "fields": [
                            {
                                "name": request.field_label,
                                "target": target,
                                "type": request.control_type,
                                "value": answer.value,
                            }
                        ]
                    },
                )
            try:
                applied = await browser.inspect_control_state(target)
            except Exception:
                refreshed = await _live_target(browser, request)
                if refreshed is None:
                    raise
                applied = await browser.inspect_control_state(refreshed)
        except Exception as exc:
            return await self._recoverable(
                result,
                tool_call_id,
                "Browser executor could not apply the validated answer: "
                f"{type(exc).__name__}: {exc}",
                field_label=request.field_label,
                target=request.target,
                value=answer.value,
            )
        expected_checked = answer.value == "true"
        if (
            request.control_type in {"checkbox", "radio"}
            and applied.has_value != expected_checked
        ) or (
            request.control_type not in {"checkbox", "radio"}
            and (not applied.has_value or applied.invalid)
        ):
            return await self._recoverable(
                result,
                tool_call_id,
                "The browser did not retain a valid value after the candidate mutation.",
                field_label=request.field_label,
                target=request.target,
                value=answer.value,
            )
        self._reset_failures(request.field_label, request.target)
        await self._remember_human_answer(request, answer)
        self._record_applied(answer)
        result_name = (
            "CANDIDATE_FIELD_TYPED"
            if request.control_type == "combobox"
            else "CANDIDATE_FIELD_APPLIED"
        )
        continuation = (
            "The deterministic browser executor typed this exact answer. If the "
            "receipt exposes a listbox, select the matching visible option before "
            "continuing."
            if request.control_type == "combobox"
            else "The deterministic browser executor applied this exact answer."
        )
        return _replace_result(
            result,
            ToolMessage(
                content=(
                    f"{result_name}\n"
                    f"{answer.model_dump_json()}\n"
                    f"{continuation} Browser-observed value: {applied.value!r}. "
                    "Continue from the receipt; do not delegate or type it again.\n"
                    f"{receipt}"
                ),
                name="task",
                tool_call_id=tool_call_id,
            ),
        )

    def _record_applied(self, answer: CandidateFieldAnswer) -> None:
        if self.on_applied is not None:
            self.on_applied(answer)

    async def _remember_human_answer(
        self,
        request: CandidateFieldRequest,
        answer: CandidateFieldAnswer,
    ) -> None:
        if answer.source != "human" or self._candidate_memory is None:
            return
        await self._candidate_memory.remember_human_answer(
            field_label=request.field_label,
            question=request.field_label,
            answer=answer.value,
        )

    async def _recoverable(
        self,
        result: ToolMessage | Command[Any],
        tool_call_id: str,
        reason: str,
        *,
        field_label: str,
        target: str,
        value: str,
    ) -> ToolMessage | Command[Any]:
        key = (field_label, target)
        self._consecutive_failures[key] = self._consecutive_failures.get(key, 0) + 1
        if self._consecutive_failures[key] >= _MAX_CANDIDATE_FIELD_ATTEMPTS:
            return _error(
                result,
                tool_call_id,
                "repeated candidate mutation failures on the same field "
                f"({_MAX_CANDIDATE_FIELD_ATTEMPTS} attempts): {reason} Re-observe the "
                "field. If it is still unresolved, delegate once more to AnswerWriter "
                "so it can ask the human for the exact value, or call application_blocked "
                "when no value can be obtained.",
            )
        return await self._recoverable_error(result, tool_call_id, reason)

    def _reset_failures(self, field_label: str, target: str) -> None:
        self._consecutive_failures.pop((field_label, target), None)

    async def _recoverable_error(
        self,
        result: ToolMessage | Command[Any],
        tool_call_id: str,
        reason: str,
    ) -> ToolMessage | Command[Any]:
        evidence = ""
        if self._browser is not None:
            try:
                evidence = await self._browser.observe()
            except Exception:
                evidence = "Fresh browser evidence is temporarily unavailable."
        return _error(
            result,
            tool_call_id,
            f"{reason} The answer was not consumed. Recover using the fresh evidence "
            f"below, or rotate the model if it repeats.\n{evidence}",
        )


def _answers(result: ToolMessage | Command[Any]) -> list[CandidateFieldAnswer]:
    answers: list[CandidateFieldAnswer] = []
    for message in _tool_messages(result):
        if message.status == "error":
            continue
        try:
            answers.append(CandidateFieldAnswer.model_validate_json(message.text))
        except ValueError:
            continue
    return answers


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


def _error(
    result: ToolMessage | Command[Any],
    tool_call_id: str,
    reason: str,
) -> ToolMessage | Command[Any]:
    return _replace_result(
        result,
        ToolMessage(
            content=f"CANDIDATE_FIELD_EXECUTION_ERROR: {reason}",
            name="task",
            tool_call_id=tool_call_id,
            status="error",
        ),
    )


def _replace_result(
    result: ToolMessage | Command[Any], message: ToolMessage
) -> ToolMessage | Command[Any]:
    if isinstance(result, ToolMessage):
        return message
    update = dict(result.update) if isinstance(result.update, dict) else {}
    update["messages"] = [message]
    return Command(
        graph=result.graph,
        update=update,
        resume=result.resume,
        goto=result.goto,
    )
