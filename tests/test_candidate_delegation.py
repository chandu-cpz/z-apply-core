from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from z_apply_core.agents.candidate_field import CandidateFieldMiddleware
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
            "constraints": [],
            "visible_options": [],
            "validation": ["This field is required"],
            "row_context": "",
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
        inspect_control_state=AsyncMock(return_value=BrowserControlState(target="e96")),
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
async def test_already_resolved_target_is_rejected_before_answer_writer_runs() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Last Name" [ref=e96]: V',
        ),
        inspect_control_state=AsyncMock(
            return_value=BrowserControlState(
                target="e96",
                value="V",
                has_value=True,
            )
        ),
    )
    middleware = CandidateFieldMiddleware(browser)
    handler = AsyncMock(
        side_effect=[
            ModelResponse(result=[AIMessage(content="", tool_calls=[_candidate_call()])]),
            ModelResponse(
                result=[
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "browser_observe", "id": "observe-1", "args": {}}],
                    )
                ]
            ),
        ]
    )

    result = await middleware.awrap_model_call(_request(), handler)

    assert result.result[0].tool_calls[0]["name"] == "browser_observe"
    correction = handler.await_args_list[1].args[0].messages[-1]
    assert "already resolved" in correction.content


@pytest.mark.asyncio
async def test_answer_writer_result_is_applied_atomically_by_browser_executor() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "First Name" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(return_value=BrowserControlState(target="e96")),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: true"),
    )
    middleware = CandidateFieldMiddleware(browser)
    normalized = middleware._normalize_call(_candidate_call())
    answer = CandidateFieldAnswer(
        field_label="Where did you hear about us?",
        target="e96",
        value="LinkedIn",
    )
    result = await middleware.awrap_tool_call(
        SimpleNamespace(tool_call=normalized),
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
async def test_atomic_candidate_failure_returns_fresh_evidence_for_recovery() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "First Name" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(return_value=BrowserControlState(target="e96")),
        call_tool_with_inline_snapshot=AsyncMock(side_effect=RuntimeError("control detached")),
        observe=AsyncMock(return_value="BROWSER OBSERVATION revision: 8"),
    )
    middleware = CandidateFieldMiddleware(browser)
    normalized = middleware._normalize_call(_candidate_call())
    answer = CandidateFieldAnswer(
        field_label="Where did you hear about us?",
        target="e96",
        value="LinkedIn",
    )
    result = await middleware.awrap_tool_call(
        SimpleNamespace(tool_call=normalized),
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
