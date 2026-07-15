from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from z_apply_core.agents.specialist_task_context import initial_specialist_task


def test_initial_specialist_task_survives_tool_turns_and_controller_feedback() -> None:
    messages = [
        HumanMessage(content="Resolve Given Name(s) from candidate evidence."),
        AIMessage(content="", tool_calls=[]),
        ToolMessage(content="resume evidence", tool_call_id="tool-1"),
        HumanMessage(content="Retry natively.", name="runtime_controller"),
    ]

    assert initial_specialist_task(messages) == (
        "Resolve Given Name(s) from candidate evidence."
    )
