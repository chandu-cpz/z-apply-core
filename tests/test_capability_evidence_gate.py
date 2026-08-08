from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from z_apply_core.agents.capability_context import (
    _last_tool_message_carries_revision,
)


def _tool_message(content: str, *, revision: int | None, error: bool = False) -> ToolMessage:
    kwargs = {"browser_revision": revision} if revision is not None else {}
    return ToolMessage(
        content=content,
        tool_call_id="call_1",
        name="browser_click",
        status="error" if error else "success",
        additional_kwargs=kwargs,
    )


def test_last_tool_message_carries_current_revision() -> None:
    messages = [
        HumanMessage(content="start"),
        AIMessage(content="", tool_calls=[{"name": "browser_click", "args": {}, "id": "call_1"}]),
        _tool_message("BROWSER ACTION RECEIPT", revision=7),
    ]
    assert _last_tool_message_carries_revision(messages, 7) is True


def test_stale_revision_does_not_count() -> None:
    messages = [
        AIMessage(content="", tool_calls=[{"name": "browser_click", "args": {}, "id": "call_1"}]),
        _tool_message("BROWSER ACTION RECEIPT", revision=6),
    ]
    assert _last_tool_message_carries_revision(messages, 7) is False


def test_non_evidence_tool_result_does_not_count() -> None:
    messages = [
        AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "call_1"}]),
        _tool_message("AnswerWriter result", revision=None),
    ]
    assert _last_tool_message_carries_revision(messages, 7) is False


def test_error_result_never_counts() -> None:
    messages = [
        AIMessage(content="", tool_calls=[{"name": "browser_click", "args": {}, "id": "call_1"}]),
        _tool_message("RUNTIME STATE-ACTION CIRCUIT", revision=7, error=True),
    ]
    assert _last_tool_message_carries_revision(messages, 7) is False


def test_no_tool_result_does_not_count() -> None:
    messages = [HumanMessage(content="start"), AIMessage(content="prose without tools")]
    assert _last_tool_message_carries_revision(messages, 7) is False


def test_none_revision_does_not_count() -> None:
    messages = [
        AIMessage(content="", tool_calls=[{"name": "browser_click", "args": {}, "id": "call_1"}]),
        _tool_message("BROWSER ACTION RECEIPT", revision=7),
    ]
    assert _last_tool_message_carries_revision(messages, None) is False
