from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import ToolMessage

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.stream_events import FrameworkTraceEvent


async def async_items(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


async def done(value: Any = None) -> Any:
    return value


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[FrameworkTraceEvent] = []

    async def accept(self, event: FrameworkTraceEvent) -> None:
        self.events.append(event)


class FakeMessage:
    def __init__(
        self,
        *,
        text: list[str] | None = None,
        reasoning: list[str] | None = None,
    ) -> None:
        self.text = async_items(text or [])
        self.reasoning = async_items(reasoning or [])
        self.tool_calls = async_items([])
        self.output = done()


class FakeStream:
    def __init__(self) -> None:
        self.messages = async_items(
            [
                FakeMessage(
                    text=["fake JSON task call"],
                    reasoning=["actual planning trace"],
                )
            ]
        )
        self.tool_calls = async_items([])
        self.subagents = async_items([FakeSubagent()])

    async def output(self) -> dict[str, Any]:
        return {}


class FakeSubagent:
    name = "BrowserSpecialist"
    status = "started"
    path = ""

    def __init__(self) -> None:
        self.messages = async_items([FakeMessage(text=["subagent evidence"])])
        self.tool_calls = async_items([])
        self.subagents = async_items([])
        self.output = done()


class FakeToolCall:
    tool_name = "task"
    tool_call_id = "call-1"
    parent_tool_call_id = ""
    input = {"subagent_type": "AnswerWriter", "description": "Resolve Skills"}
    output_deltas = async_items([])
    output = ToolMessage(
        content={"answer": "Skills = Python, FastAPI"},
        tool_call_id="call-1",
    )
    error = None
    completed = True


class FakeToolStream(FakeStream):
    def __init__(self) -> None:
        super().__init__()
        self.tool_calls = async_items([FakeToolCall()])


class DeepAgentStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_orchestrator_text_deltas_are_streamed(self) -> None:
        sink = CollectingSink()

        await consume_deepagent_stream(FakeStream(), sink=sink)

        rendered_text = [
            event.data.get("delta")
            for event in sink.events
            if event.event == "agent_message_delta" and event.data.get("kind") == "text"
        ]
        rendered_reasoning = [
            event.data.get("delta")
            for event in sink.events
            if event.event == "agent_message_delta" and event.data.get("kind") == "reasoning"
        ]

        self.assertIn("fake JSON task call", rendered_text)
        self.assertIn("subagent evidence", rendered_text)
        self.assertIn("actual planning trace", rendered_reasoning)

    async def test_authentication_controller_text_deltas_are_streamed(self) -> None:
        sink = CollectingSink()

        await consume_deepagent_stream(
            FakeStream(),
            sink=sink,
            root_source="authenticate_default_account",
        )

        rendered_text = [
            event.data.get("delta")
            for event in sink.events
            if event.event == "agent_message_delta" and event.data.get("kind") == "text"
        ]
        self.assertIn("fake JSON task call", rendered_text)

    async def test_completed_tool_event_contains_normalized_authoritative_output(self) -> None:
        sink = CollectingSink()

        await consume_deepagent_stream(FakeToolStream(), sink=sink)

        completed = next(event for event in sink.events if event.event == "agent_tool_end")
        self.assertEqual(
            completed.data["output"],
            {
                "content": "{'answer': 'Skills = Python, FastAPI'}",
                "status": "success",
            },
        )


if __name__ == "__main__":
    unittest.main()
