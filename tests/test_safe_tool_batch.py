from __future__ import annotations

from langchain_core.messages import AIMessage

from z_apply_core.agents.safe_tool_batch import SafeToolBatchMiddleware


def _call(name: str, index: int, **args: object) -> dict[str, object]:
    return {"name": name, "args": args, "id": f"call-{index}", "type": "tool_call"}


def test_serializes_answer_writer_and_mixed_tool_batches() -> None:
    middleware = SafeToolBatchMiddleware()
    tasks = [
        _call("task", index, subagent_type="AnswerWriter", description=f"field {index}")
        for index in range(5)
    ]

    answer_batch = middleware._safe_batch(AIMessage(content="", tool_calls=tasks))
    mixed_batch = SafeToolBatchMiddleware()._safe_batch(
        AIMessage(
            content="",
            tool_calls=[tasks[0], _call("browser_snapshot", 9)],
        )
    )

    assert len(answer_batch.tool_calls) == 1
    assert len(mixed_batch.tool_calls) == 1
