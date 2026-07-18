from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from z_apply_core.agents.specialists.answer_writer import (
    CandidateFieldAnswer,
    CandidateFieldRequest,
)
from z_apply_core.browser_session import BrowserSession


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
    observation = browser.current_observation
    if observation is None or observation.revision != request.browser_revision:
        return (
            "CANDIDATE DELEGATION ERROR: browser_revision is stale or unavailable. "
            "Discard the request and call browser_observe for fresh evidence."
        )
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
    if control.has_value and not control.invalid:
        return (
            "CANDIDATE DELEGATION ERROR: the live target is already resolved. Do "
            "not delegate or overwrite it; continue with an unresolved field."
        )
    if request.current_value != control.value:
        return (
            "CANDIDATE DELEGATION ERROR: current_value does not match the live "
            "target. Discard stale evidence and observe the browser again."
        )
    return None


class CandidateFieldExecutor:
    """Apply one AnswerWriter result or return recoverable browser evidence."""

    def __init__(self, browser: BrowserSession | None) -> None:
        self._browser = browser

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
        if await candidate_browser_violation(browser, request) is not None:
            return await self._recoverable_error(
                result,
                tool_call_id,
                "The browser field changed before its answer could be applied.",
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
        except Exception as exc:
            return await self._recoverable_error(
                result,
                tool_call_id,
                "Browser executor could not apply the validated answer: "
                f"{type(exc).__name__}: {exc}",
            )
        return _replace_result(
            result,
            ToolMessage(
                content=(
                    "CANDIDATE_FIELD_APPLIED\n"
                    f"{answer.model_dump_json()}\n"
                    "The deterministic browser executor applied this exact answer. "
                    "Continue from the receipt; do not apply it again.\n"
                    f"{receipt}"
                ),
                name="task",
                tool_call_id=tool_call_id,
            ),
        )

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
