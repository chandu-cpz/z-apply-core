from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse, ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from z_apply_core.agents.candidate_field import CandidateFieldMiddleware
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.agents.specialists.answer_writer import CandidateFieldAnswer
from z_apply_core.browser_observation import BrowserControlState, BrowserObservation


def _request() -> ModelRequest[Any]:
    return ModelRequest(model=object(), messages=[], tools=[])


def _candidate_call(*, call_id: str = "candidate-1") -> dict[str, Any]:
    return {
        "name": "resolve_candidate_field",
        "id": call_id,
        "args": {
            "browser_revision": 7,
            "field_label": "Where did you hear about us?",
            "target": "e96",
            "current_value": "",
            "control_type": "textbox",
        },
    }


@pytest.mark.asyncio
async def test_free_text_answer_writer_handoff_is_corrected_to_typed_request() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Type your response" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(return_value=BrowserControlState()),
    )
    middleware = CandidateFieldMiddleware(browser)
    responses = [
        ModelResponse(
            result=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "id": "raw-1",
                            "args": {
                                "subagent_type": "AnswerWriter",
                                "description": "Answer e96 with LinkedIn.",
                            },
                        }
                    ],
                )
            ]
        ),
        ModelResponse(result=[AIMessage(content="", tool_calls=[_candidate_call()])]),
    ]
    handler = AsyncMock(side_effect=responses)

    result = await middleware.awrap_model_call(_request(), handler)

    normalized = result.result[0].tool_calls[0]
    assert normalized["name"] == "task"
    assert normalized["args"]["subagent_type"] == "AnswerWriter"
    assert '"target": "e96"' in normalized["args"]["description"]
    assert "LinkedIn" not in normalized["args"]["description"]
    assert handler.await_count == 2


@pytest.mark.asyncio
async def test_prefilled_target_can_be_reconciled_by_answer_writer() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Last Name" [ref=e96]: V',
        ),
        inspect_control_state=AsyncMock(
            return_value=BrowserControlState(value="V", has_value=True)
        ),
    )
    middleware = CandidateFieldMiddleware(browser)
    call = _candidate_call()
    call["args"]["current_value"] = "V"
    handler = AsyncMock(
        return_value=ModelResponse(result=[AIMessage(content="", tool_calls=[call])])
    )

    result = await middleware.awrap_model_call(_request(), handler)

    assert result.result[0].tool_calls[0]["name"] == "task"
    assert '"current_value": "V"' in result.result[0].tool_calls[0]["args"]["description"]
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_answer_writer_result_is_applied_atomically_by_browser_executor() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "First Name" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(
            side_effect=[
                BrowserControlState(),
                BrowserControlState(),
                BrowserControlState(value="LinkedIn", has_value=True),
            ]
        ),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: true"),
    )
    candidate_memory = SimpleNamespace(
        lookup=AsyncMock(
            return_value={
                "memory_status": "exact",
                "field_label": "Where did you hear about us?",
                "question": "Where did you hear about us?",
                "matches": [{"answer": "LinkedIn", "source": "human_answer"}],
            }
        )
    )
    middleware = CandidateFieldMiddleware(browser, candidate_memory)
    normalized = middleware._normalize_call(_candidate_call())
    answer = CandidateFieldAnswer(
        source="memory",
        field_label="Where did you hear about us?",
        target="e96",
        value="LinkedIn",
    )
    handler = AsyncMock(
        return_value=Command(
            update={
                "messages": [
                    ToolMessage(
                        answer.model_dump_json(),
                        tool_call_id="candidate-1",
                    )
                ]
            }
        )
    )
    result = await middleware.awrap_tool_call(
        ToolCallRequest(tool_call=normalized, tool=None, state={}, runtime=object()),  # type: ignore[arg-type]
        handler,
    )

    candidate_memory.lookup.assert_awaited_once_with(
        field_label="Where did you hear about us?",
        question="Where did you hear about us?",
    )
    forwarded_request = handler.await_args.args[0]
    assert '"memory_status": "exact"' in forwarded_request.tool_call["args"]["description"]
    browser.call_tool_with_inline_snapshot.assert_awaited_once_with(
        "browser_fill_form",
        {
            "fields": [
                {
                    "name": "Where did you hear about us?",
                    "target": "e96",
                    "type": "textbox",
                    "value": "LinkedIn",
                }
            ]
        },
    )
    message = result.update["messages"][0]
    assert message.status == "success"
    assert "CANDIDATE_FIELD_APPLIED" in message.text
    assert "changed: true" in message.text


@pytest.mark.asyncio
async def test_normalized_human_answer_is_stored_after_browser_mutation() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Email" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(
            side_effect=[
                BrowserControlState(),
                BrowserControlState(),
                BrowserControlState(value="candidate@example.com", has_value=True),
            ]
        ),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: true"),
    )
    candidate_memory = SimpleNamespace(
        lookup=AsyncMock(return_value={"memory_status": "no_exact_match", "matches": []}),
        remember_human_answer=AsyncMock(return_value=True),
    )
    middleware = CandidateFieldMiddleware(browser, candidate_memory)
    normalized = middleware._normalize_call(_candidate_call())
    answer = CandidateFieldAnswer(
        source="human",
        field_label="Where did you hear about us?",
        target="e96",
        value="candidate@example.com",
    )

    result = await middleware.awrap_tool_call(
        ToolCallRequest(tool_call=normalized, tool=None, state={}, runtime=object()),  # type: ignore[arg-type]
        AsyncMock(
            return_value=Command(
                update={
                    "messages": [
                        ToolMessage(answer.model_dump_json(), tool_call_id="candidate-1")
                    ]
                }
            )
        ),
    )

    browser.call_tool_with_inline_snapshot.assert_awaited_once_with(
        "browser_fill_form",
        {
            "fields": [
                {
                    "name": "Where did you hear about us?",
                    "target": "e96",
                    "type": "textbox",
                    "value": "candidate@example.com",
                }
            ]
        },
    )
    candidate_memory.remember_human_answer.assert_awaited_once_with(
        field_label="Where did you hear about us?",
        question="Where did you hear about us?",
        answer="candidate@example.com",
    )
    message = result.update["messages"][0]
    assert "candidate@example.com" in message.text


@pytest.mark.asyncio
async def test_combobox_value_must_survive_the_browser_mutation() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='combobox "Location (City)" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(
            side_effect=[
                BrowserControlState(),
                BrowserControlState(),
                BrowserControlState(),
            ]
        ),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: false"),
        inspect_control_options=AsyncMock(
            return_value=("Hyderabad, Telangana, India",)
        ),
        observe=AsyncMock(return_value="BROWSER OBSERVATION revision: 8"),
    )
    middleware = CandidateFieldMiddleware(browser)
    call = _candidate_call()
    call["args"].update(
        field_label="Location (City)*",
        control_type="combobox",
    )
    normalized = middleware._normalize_call(call)
    answer = CandidateFieldAnswer(
        source="resume",
        field_label="Location (City)*",
        target="e96",
        value="Hyderabad",
    )

    result = await middleware.awrap_tool_call(
        ToolCallRequest(tool_call=normalized, tool=None, state={}, runtime=object()),  # type: ignore[arg-type]
        AsyncMock(
            return_value=Command(
                update={
                    "messages": [
                        ToolMessage(answer.model_dump_json(), tool_call_id="candidate-1")
                    ]
                }
            )
        ),
    )

    message = result.update["messages"][0]
    browser.call_tool_with_inline_snapshot.assert_awaited_once_with(
        "browser_type",
        {"target": "e96", "text": "Hyderabad"},
    )
    assert message.status == "error"
    assert "did not retain a valid value" in message.text
    assert "BROWSER OBSERVATION revision: 8" in message.text


@pytest.mark.asyncio
async def test_repeated_candidate_violation_reports_the_rejected_browser_fact() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Last Name" [ref=e96]: V',
        ),
        inspect_control_state=AsyncMock(
            return_value=BrowserControlState(value="V", has_value=True)
        ),
    )
    middleware = CandidateFieldMiddleware(browser)
    handler = AsyncMock(
        return_value=ModelResponse(
            result=[AIMessage(content="", tool_calls=[_candidate_call()])]
        )
    )

    with pytest.raises(ToolProtocolViolation, match="does not match"):
        await middleware.awrap_model_call(_request(), handler)

    assert handler.await_count == 2


@pytest.mark.asyncio
async def test_atomic_candidate_failure_returns_fresh_evidence_for_recovery() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "First Name" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(return_value=BrowserControlState()),
        call_tool_with_inline_snapshot=AsyncMock(side_effect=RuntimeError("control detached")),
        observe=AsyncMock(return_value="BROWSER OBSERVATION revision: 8"),
    )
    middleware = CandidateFieldMiddleware(browser)
    normalized = middleware._normalize_call(_candidate_call())
    answer = CandidateFieldAnswer(
        source="memory",
        field_label="Where did you hear about us?",
        target="e96",
        value="LinkedIn",
    )
    result = await middleware.awrap_tool_call(
        ToolCallRequest(tool_call=normalized, tool=None, state={}, runtime=object()),  # type: ignore[arg-type]
        AsyncMock(
            return_value=Command(
                update={
                    "messages": [
                        ToolMessage(
                            answer.model_dump_json(),
                            tool_call_id="candidate-1",
                        )
                    ]
                }
            )
        ),
    )

    message = result.update["messages"][0]
    assert message.status == "error"
    assert "The answer was not consumed" in message.text
    assert "BROWSER OBSERVATION revision: 8" in message.text
