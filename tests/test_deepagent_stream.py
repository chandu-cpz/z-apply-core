from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

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


class FakeReceiptToolCall:
    tool_name = "task"
    tool_call_id = "call-2"
    parent_tool_call_id = ""
    input = {"subagent_type": "AnswerWriter", "description": "Resolve one field"}
    output_deltas = async_items([])
    output = Command(
        update={
            "messages": [
                ToolMessage(
                    content=(
                        "CANDIDATE_FIELD_EXECUTION_ERROR: the browser did not retain a valid value."
                    ),
                    tool_call_id="call-2",
                )
            ]
        }
    )
    error = None
    completed = True


class FakeToolReceiptStream(FakeStream):
    def __init__(self) -> None:
        super().__init__()
        self.tool_calls = async_items([FakeReceiptToolCall()])


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

    async def test_task_command_receipt_is_surfaced_in_stream(self) -> None:
        sink = CollectingSink()

        await consume_deepagent_stream(FakeToolReceiptStream(), sink=sink)

        ended = next(event for event in sink.events if event.event == "agent_tool_end")
        output = ended.data["output"]
        self.assertEqual(output["status"], "success")
        self.assertIn("CANDIDATE_FIELD_EXECUTION_ERROR", output["content"])


class FakeToolChunk:
    def __init__(self, index: int, name: str, args: str, tool_id: str = "") -> None:
        self.tool_call_index = index
        self.name = name
        self.args = args
        self.id = tool_id


class FakeMessageWithToolChunks(FakeMessage):
    def __init__(self, chunks: list[FakeToolChunk]) -> None:
        super().__init__()
        self.tool_calls = async_items(chunks)


class EmptyToolChunkTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_tool_call_chunks_are_skipped_but_named_ones_kept(self) -> None:
        sink = CollectingSink()
        message = FakeMessageWithToolChunks(
            [
                FakeToolChunk(index=0, name="", args="", tool_id=""),
                FakeToolChunk(index=0, name="browser_click", args="", tool_id="call-1"),
                FakeToolChunk(index=0, name="", args='{"target": "e23"}'),
            ]
        )
        fake_stream = SimpleNamespace(
            messages=async_items([message]),
            tool_calls=async_items([]),
            subagents=async_items([]),
            output=done({}),
        )
        await consume_deepagent_stream(fake_stream, sink=sink, root_source="orchestrator")

        turn_events = [event for event in sink.events if event.event == "agent_turn"]
        self.assertEqual(len(turn_events), 1)
        tool_calls = turn_events[0].data["tool_calls"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["name"], "browser_click")
        self.assertEqual(tool_calls[0]["args"], '{"target": "e23"}')

    async def test_turn_with_only_empty_chunks_is_dropped_entirely(self) -> None:
        sink = CollectingSink()
        message = FakeMessageWithToolChunks([FakeToolChunk(index=0, name="", args="", tool_id="")])
        fake_stream = SimpleNamespace(
            messages=async_items([message]),
            tool_calls=async_items([]),
            subagents=async_items([]),
            output=done({}),
        )
        await consume_deepagent_stream(fake_stream, sink=sink, root_source="orchestrator")

        # A turn that only carried an empty placeholder chunk has no content
        # (no text, no reasoning, no named tool call) and is dropped entirely.
        turn_events = [event for event in sink.events if event.event == "agent_turn"]
        self.assertEqual(turn_events, [])


if __name__ == "__main__":
    unittest.main()
