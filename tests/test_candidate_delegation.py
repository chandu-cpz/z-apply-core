from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse, ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

from z_apply_core.agents.candidate_field import CandidateFieldMiddleware
from z_apply_core.agents.candidate_field_executor import (
    CandidateFieldExecutor,
    candidate_browser_violation,
)
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.agents.specialists.answer_writer import (
    CandidateFieldAnswer,
    CandidateFieldRequest,
)
from z_apply_core.browser_observation import BrowserControlState, BrowserObservation
from z_apply_core.browser_session import BrowserSession


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


def test_candidate_field_request_accepts_legacy_and_versioned_targets() -> None:
    for target in ("e140", "e140v11", "f1e2v5"):
        request = CandidateFieldRequest.model_validate(
            {
                "browser_revision": 7,
                "field_label": "Where did you hear about us?",
                "target": target,
                "current_value": "",
                "control_type": "textbox",
            }
        )
        assert request.target == target


def test_candidate_field_request_rejects_malformed_targets() -> None:
    for target in ("e140vv11", "e140!", "e", "v11", "[ref=e140]"):
        with pytest.raises(ValidationError) as exc_info:
            CandidateFieldRequest.model_validate(
                {
                    "browser_revision": 7,
                    "field_label": "Where did you hear about us?",
                    "target": target,
                    "current_value": "",
                    "control_type": "textbox",
                }
            )
        assert any(error["loc"] == ("target",) for error in exc_info.value.errors())


def test_candidate_field_answer_accepts_legacy_and_versioned_targets() -> None:
    for target in ("e140", "e140v11", "f1e2v5"):
        answer = CandidateFieldAnswer(
            source="memory",
            field_label="Where did you hear about us?",
            target=target,
            value="LinkedIn",
        )
        assert answer.target == target


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
async def test_masked_current_value_skips_stale_equality_gate() -> None:
    browser = SimpleNamespace(
        inspect_control_state=AsyncMock(
            return_value=BrowserControlState(value="different-live-value", has_value=True)
        ),
    )
    request = CandidateFieldRequest(
        browser_revision=7,
        field_label="Email",
        target="e96",
        current_value="<secret>DEFAULT_USERNAME</secret>",
        control_type="textbox",
    )

    violation = await candidate_browser_violation(browser, request, target="e96")

    assert violation is None


@pytest.mark.asyncio
async def test_unmasked_value_mismatch_still_reports_violation() -> None:
    browser = SimpleNamespace(
        inspect_control_state=AsyncMock(
            return_value=BrowserControlState(value="different-live-value", has_value=True)
        ),
    )
    request = CandidateFieldRequest(
        browser_revision=7,
        field_label="Email",
        target="e96",
        current_value="user@example.com",
        control_type="textbox",
    )

    violation = await candidate_browser_violation(browser, request, target="e96")

    assert violation is not None
    assert "does not match the live" in violation


@pytest.mark.asyncio
async def test_masked_live_value_is_confirmed_as_already_filled() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Email" [ref=e96]: <secret>DEFAULT_USERNAME</secret>',
        ),
        inspect_control_state=AsyncMock(
            return_value=BrowserControlState(
                value="<secret>DEFAULT_USERNAME</secret>", has_value=True
            )
        ),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: true"),
    )
    executor = CandidateFieldExecutor(browser)
    request = CandidateFieldRequest(
        browser_revision=7,
        field_label="Email",
        target="e96",
        current_value="<secret>DEFAULT_USERNAME</secret>",
        control_type="textbox",
    )
    answer = CandidateFieldAnswer(
        source="memory",
        field_label="Email",
        target="e96",
        value="someone@example.com",
    )

    result = await executor.apply(
        ToolCallRequest(
            tool_call={
                "name": "task",
                "id": "call-masked",
                "args": {"subagent_type": "AnswerWriter", "description": ""},
            },
            tool=None,
            state={},
            runtime=object(),
        ),  # type: ignore[arg-type]
        Command(
            update={
                "messages": [
                    ToolMessage(
                        answer.model_dump_json(),
                        tool_call_id="call-masked",
                    )
                ]
            }
        ),
        request,
    )

    message = result.update["messages"][0]
    assert message.status == "success"
    assert "CANDIDATE_FIELD_CONFIRMED" in message.text
    assert "already contains a masked value" in message.text
    browser.call_tool_with_inline_snapshot.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_stale_target_ref_is_re_resolved_by_label_before_apply() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Where did you hear about us?" [ref=e96v5]',
        ),
        inspect_control_state=AsyncMock(
            side_effect=[
                RuntimeError("STALE_SNAPSHOT: ref from old version"),
                BrowserControlState(),
                BrowserControlState(),
                BrowserControlState(value="LinkedIn", has_value=True),
            ]
        ),
        resolve_control_ref=AsyncMock(return_value="e99v22"),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: true"),
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
                        ToolMessage(answer.model_dump_json(), tool_call_id="candidate-1")
                    ]
                }
            )
        ),
    )

    browser.resolve_control_ref.assert_awaited_once_with(
        "Where did you hear about us?"
    )
    fill_args = browser.call_tool_with_inline_snapshot.await_args.args
    assert fill_args[0] == "browser_fill_form"
    assert fill_args[1]["fields"][0]["target"] == "e99v22"
    message = result.update["messages"][0]
    assert message.status == "success"
    assert "CANDIDATE_FIELD_APPLIED" in message.text


@pytest.mark.asyncio
async def test_resolve_control_ref_matches_label_across_evidence_lines() -> None:
    browser = BrowserSession.__new__(BrowserSession)
    browser.call_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            "- document [ref=e20v22]:\n"
            '  - textbox "First Name" [active] [ref=e70v22]: Chandrakanth\n'
            '  - combobox "Country selector" [ref=e87v22] [cursor=pointer]\n'
            "  - textbox [ref=e90v22]: +91\n"
        )
    )

    assert await browser.resolve_control_ref("First Name") == "e70v22"
    assert await browser.resolve_control_ref("first name*") == "e70v22"
    assert await browser.resolve_control_ref("Country selector") == "e87v22"
    assert await browser.resolve_control_ref("Unknown Field") is None


@pytest.mark.asyncio
async def test_identity_mismatch_falls_back_to_evidence_verified_ref() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Where did you hear about us?" [ref=e96v5]',
        ),
        inspect_control_state=AsyncMock(
            side_effect=[
                BrowserControlState(control_name="phone"),
                BrowserControlState(control_name="phone"),
                BrowserControlState(control_name="phone"),
                BrowserControlState(control_name="phone"),
                BrowserControlState(control_name="phone", value="LinkedIn", has_value=True),
            ]
        ),
        resolve_control_ref=AsyncMock(return_value="e70v9"),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: true"),
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
                        ToolMessage(answer.model_dump_json(), tool_call_id="candidate-1")
                    ]
                }
            )
        ),
    )

    browser.resolve_control_ref.assert_awaited_once_with(
        "Where did you hear about us?"
    )
    fill_args = browser.call_tool_with_inline_snapshot.await_args.args
    assert fill_args[0] == "browser_fill_form"
    assert fill_args[1]["fields"][0]["target"] == "e70v9"
    message = result.update["messages"][0]
    assert message.status == "success"
    assert "CANDIDATE_FIELD_APPLIED" in message.text


class _ReceiptSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def accept(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_candidate_field_receipt_event_is_emitted() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Where did you hear about us?" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(
            side_effect=[
                BrowserControlState(),
                BrowserControlState(),
                BrowserControlState(),
                BrowserControlState(value="LinkedIn", has_value=True),
            ]
        ),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: true"),
    )
    sink = _ReceiptSink()
    middleware = CandidateFieldMiddleware(browser, sink=sink)
    normalized = middleware._normalize_call(_candidate_call())
    answer = CandidateFieldAnswer(
        source="memory",
        field_label="Where did you hear about us?",
        target="e96",
        value="LinkedIn",
    )

    await middleware.awrap_tool_call(
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

    receipts = [
        event for event in sink.events if getattr(event, "event", "") == "candidate_field_receipt"
    ]
    assert len(receipts) == 1
    assert receipts[0].data["verdict"] == "CANDIDATE_FIELD_APPLIED"
    assert receipts[0].data["field_label"] == "Where did you hear about us?"


@pytest.mark.asyncio
async def test_repeated_failures_on_same_field_escalate_after_cap() -> None:
    browser = SimpleNamespace(
        current_observation=BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "Expected Salary" [ref=e96]',
        ),
        inspect_control_state=AsyncMock(return_value=BrowserControlState()),
        call_tool_with_inline_snapshot=AsyncMock(return_value="changed: false"),
        observe=AsyncMock(return_value="BROWSER OBSERVATION revision: 8"),
    )
    executor = CandidateFieldExecutor(browser)
    request = CandidateFieldRequest(
        browser_revision=7,
        field_label="Expected Salary",
        target="e96",
        current_value="",
        control_type="textbox",
    )
    messages: list[ToolMessage] = []
    for index, value in enumerate(("6 LPA", "600000", "600000 per annum")):
        answer = CandidateFieldAnswer(
            source="resume",
            field_label="Expected Salary",
            target="e96",
            value=value,
        )
        result = await executor.apply(
            ToolCallRequest(
                tool_call={
                    "name": "task",
                    "id": f"call-{index}",
                    "args": {"subagent_type": "AnswerWriter", "description": ""},
                },
                tool=None,
                state={},
                runtime=object(),
            ),  # type: ignore[arg-type]
            Command(
                update={
                    "messages": [
                        ToolMessage(answer.model_dump_json(), tool_call_id=f"call-{index}")
                    ]
                }
            ),
            request,
        )
        messages.append(result.update["messages"][0])

    assert messages[0].status == "error"
    assert "The answer was not consumed" in messages[0].content
    assert messages[1].status == "error"
    assert "The answer was not consumed" in messages[1].content
    assert messages[2].status == "error"
    assert "repeated candidate mutation failures on the same field (3 attempts)" in messages[2].content
    assert "application_blocked" in messages[2].content


class _SerializingBrowser:
    """Fake browser that records whether mutations overlap in time."""

    def __init__(self) -> None:
        self.current_observation = BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence='textbox "First Name" [ref=e96]',
        )
        self._inspect_counts: dict[str, int] = {}
        self.active_mutations = 0
        self.max_concurrent_mutations = 0
        self.applied_targets: list[str] = []

    async def inspect_control_state(self, target: str) -> BrowserControlState:
        count = self._inspect_counts.get(target, 0) + 1
        self._inspect_counts[target] = count
        if count >= 4:
            return BrowserControlState(value="resolved", has_value=True)
        return BrowserControlState()

    async def call_tool_with_inline_snapshot(
        self, name: str, arguments: dict[str, Any]
    ) -> str:
        if name == "browser_fill_form":
            target = arguments["fields"][0]["target"]
        else:
            target = arguments["target"]
        self.active_mutations += 1
        self.max_concurrent_mutations = max(
            self.max_concurrent_mutations, self.active_mutations
        )
        await asyncio.sleep(0.02)
        self.applied_targets.append(target)
        self.active_mutations -= 1
        return "changed: true"


@pytest.mark.asyncio
async def test_parallel_answer_applies_complete_without_blocking_dispatch() -> None:
    browser = _SerializingBrowser()
    executor = CandidateFieldExecutor(browser)
    requests = [
        CandidateFieldRequest(
            browser_revision=7,
            field_label=f"Field {index}",
            target=f"e9{index}",
            current_value="",
            control_type="textbox",
        )
        for index in range(2)
    ]
    answers = [
        CandidateFieldAnswer(
            source="memory",
            field_label=f"Field {index}",
            target=f"e9{index}",
            value=f"value-{index}",
        )
        for index in range(2)
    ]

    async def apply_one(index: int) -> Command[Any]:
        tool_call_id = f"call-{index}"
        return await executor.apply(
            ToolCallRequest(
                tool_call={
                    "name": "task",
                    "id": tool_call_id,
                    "args": {"subagent_type": "AnswerWriter", "description": ""},
                },
                tool=None,
                state={},
                runtime=object(),
            ),  # type: ignore[arg-type]
            Command(
                update={
                    "messages": [
                        ToolMessage(
                            answers[index].model_dump_json(),
                            tool_call_id=tool_call_id,
                        )
                    ]
                }
            ),
            requests[index],
        )

    results = await asyncio.gather(apply_one(0), apply_one(1))

    assert browser.max_concurrent_mutations == 2
    assert sorted(browser.applied_targets) == ["e90", "e91"]
    for result in results:
        message = result.update["messages"][0]
        assert message.status == "success"
        assert "CANDIDATE_FIELD_APPLIED" in message.text
