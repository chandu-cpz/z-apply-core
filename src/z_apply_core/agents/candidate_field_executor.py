from __future__ import annotations

import asyncio
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

if TYPE_CHECKING:
    from z_apply_core.memory.applicant_memory import CandidateMemory


async def candidate_browser_violation(
    browser: BrowserSession | None,
    request: CandidateFieldRequest,
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
    try:
        control = await browser.inspect_control_state(request.target)
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
    if request.current_value != control.value:
        return (
            "CANDIDATE DELEGATION ERROR: current_value does not match the live "
            "target. Discard stale evidence and observe the browser again."
        )
    return None


def _identity_match(control_name: str, field_label: str) -> bool:
    """Return True when a live control name can support the requested label.

    An empty or non-informative control name cannot be verified and passes to avoid
    false negatives on unnamed controls. Only a definitive mismatch (both sides
    informative with no text overlap) fails.
    """
    normalized_name = _normalize_label_text(control_name)
    normalized_label = _normalize_label_text(field_label)
    if not normalized_name or not normalized_label:
        return True
    return normalized_label in normalized_name or normalized_name in normalized_label


def _normalize_label_text(text: str) -> str:
    if text.casefold().strip() == "unnamed control":
        return ""
    return " ".join(text.casefold().split())


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
        self._apply_lock = asyncio.Lock()
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
        async with self._apply_lock:
            if await candidate_browser_violation(browser, request) is not None:
                return await self._recoverable(
                    result,
                    tool_call_id,
                    "The browser field changed before its answer could be applied.",
                    target=request.target,
                    value=answer.value,
                )
            current = await browser.inspect_control_state(request.target)
            if not _identity_match(current.control_name, request.field_label):
                return await self._recoverable(
                    result,
                    tool_call_id,
                    "The browser target now resolves to a different control than the "
                    "requested field label.",
                    target=request.target,
                    value=answer.value,
                )
            if current.value == answer.value and current.has_value and not current.invalid:
                self._reset_failures(request.target, answer.value)
                await self._remember_human_answer(request, answer)
                self._record_applied(answer)
                return _replace_result(
                    result,
                    ToolMessage(
                        content=(
                            "CANDIDATE_FIELD_CONFIRMED\n"
                            f"{answer.model_dump_json()}\n"
                            "The browser already contains this exact evidence-backed value. "
                            "Do not rewrite or delegate it again."
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
                        {"target": request.target, "text": answer.value},
                    )
                else:
                    receipt = await browser.call_tool_with_inline_snapshot(
                        "browser_fill_form",
                        {
                            "fields": [
                                {
                                    "name": request.field_label,
                                    "target": request.target,
                                    "type": request.control_type,
                                    "value": answer.value,
                                }
                            ]
                        },
                    )
                applied = await browser.inspect_control_state(request.target)
            except Exception as exc:
                return await self._recoverable(
                    result,
                    tool_call_id,
                    "Browser executor could not apply the validated answer: "
                    f"{type(exc).__name__}: {exc}",
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
                    target=request.target,
                    value=answer.value,
                )
            self._reset_failures(request.target, answer.value)
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
        target: str,
        value: str,
    ) -> ToolMessage | Command[Any]:
        key = (target, value)
        self._consecutive_failures[key] = self._consecutive_failures.get(key, 0) + 1
        if self._consecutive_failures[key] >= 2:
            return _error(
                result,
                tool_call_id,
                f"repeated identical candidate mutation failure; not retrying: {reason}",
            )
        return await self._recoverable_error(result, tool_call_id, reason)

    def _reset_failures(self, target: str, value: str) -> None:
        self._consecutive_failures.pop((target, value), None)

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
