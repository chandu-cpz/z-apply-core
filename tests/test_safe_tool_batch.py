from __future__ import annotations

from langchain_core.messages import AIMessage

from z_apply_core.agents.safe_tool_batch import SafeToolBatchMiddleware


def _call(name: str, index: int, **args: object) -> dict[str, object]:
    return {"name": name, "args": args, "id": f"call-{index}", "type": "tool_call"}


def test_preserves_parallel_answer_writer_task_batches() -> None:
    middleware = SafeToolBatchMiddleware()
    tasks = [
        _call("task", index, subagent_type="AnswerWriter", description=f"field {index}")
        for index in range(5)
    ]

    batch = middleware._safe_batch(AIMessage(content="", tool_calls=tasks))

    assert len(batch.tool_calls) == 5
    assert [call["name"] for call in batch.tool_calls] == ["task"] * 5


def test_preserves_parallel_candidate_resolution_call_batches() -> None:
    middleware = SafeToolBatchMiddleware()
    calls = [
        _call(
            "resolve_candidate_field",
            index,
            browser_revision=7,
            field_label=f"Field {index}",
            target=f"e9{index}",
            current_value="",
            control_type="textbox",
        )
        for index in range(3)
    ]

    batch = middleware._safe_batch(AIMessage(content="", tool_calls=calls))

    assert len(batch.tool_calls) == 3
    assert [call["name"] for call in batch.tool_calls] == ["resolve_candidate_field"] * 3


def test_truncates_batches_containing_browser_mutations_to_first_call() -> None:
    middleware = SafeToolBatchMiddleware()
    mixed_batch = middleware._safe_batch(
        AIMessage(
            content="",
            tool_calls=[
                _call("task", 0, subagent_type="AnswerWriter", description="field 0"),
                _call("browser_fill_form", 1, fields=[]),
            ],
        )
    )

    assert len(mixed_batch.tool_calls) == 1
    assert mixed_batch.tool_calls[0]["name"] == "task"


def test_truncates_batches_mixing_candidate_and_non_candidate_calls() -> None:
    middleware = SafeToolBatchMiddleware()
    mixed_batch = middleware._safe_batch(
        AIMessage(
            content="",
            tool_calls=[
                _call(
                    "resolve_candidate_field",
                    0,
                    browser_revision=7,
                    field_label="Field 0",
                    target="e90",
                    current_value="",
                    control_type="textbox",
                ),
                _call("browser_snapshot", 1, target="html"),
            ],
        )
    )

    assert len(mixed_batch.tool_calls) == 1
    assert mixed_batch.tool_calls[0]["name"] == "resolve_candidate_field"
