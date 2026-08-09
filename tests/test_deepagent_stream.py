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


class FakeToolCallsProjection:
    """Mimic langchain-core's ``AsyncProjection``: iterable chunk deltas + awaitable final list.

    Iteration yields the raw ``ToolCallChunk`` dicts the framework pushes; awaiting
    returns the framework-finalized ``ToolCall`` dicts (sticky id/name, args parsed).
    """

    def __init__(
        self,
        chunks: list[dict[str, Any]],
        finalized: list[dict[str, Any]] | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._finalized = list(finalized) if finalized is not None else [dict(c) for c in chunks]

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return async_items(self._chunks)

    def __await__(self) -> Any:
        async def _final() -> list[dict[str, Any]]:
            return self._finalized

        return _final().__await__()


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
        self.tool_calls = FakeToolCallsProjection([])
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


class FakeMessageWithToolChunks(FakeMessage):
    def __init__(
        self,
        chunks: list[dict[str, Any]],
        finalized: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.tool_calls = FakeToolCallsProjection(chunks, finalized=finalized)


class EmptyToolChunkTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_chunks_relay_real_dict_values(self) -> None:
        # Regression: the framework pushes ``ToolCallChunk`` dicts onto the
        # projection, not objects — ``getattr`` on them previously zeroed every
        # field and silently dropped the deltas. Read them as dicts.
        sink = CollectingSink()
        message = FakeMessageWithToolChunks(
            [{"index": 0, "id": "call-1", "name": "browser_click", "args": '{"target": "e23"}'}],
            finalized=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "browser_click",
                    "args": {"target": "e23"},
                }
            ],
        )
        fake_stream = SimpleNamespace(
            messages=async_items([message]),
            tool_calls=async_items([]),
            subagents=async_items([]),
            output=done({}),
        )
        await consume_deepagent_stream(fake_stream, sink=sink, root_source="orchestrator")

        deltas = [e for e in sink.events if e.event == "agent_model_tool_call"]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].data["id"], "call-1")
        self.assertEqual(deltas[0].data["name"], "browser_click")
        self.assertEqual(deltas[0].data["args"], '{"target": "e23"}')

        turn = next(e for e in sink.events if e.event == "agent_turn")
        self.assertEqual(
            turn.data["tool_calls"],
            [
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "browser_click",
                    "args": {"target": "e23"},
                }
            ],
        )

    async def test_await_yields_finalized_tool_call_from_idless_fragments(self) -> None:
        # The turn record comes from awaiting the projection: the framework
        # merges id/name sticky and concatenates + JSON-parses args fragments.
        # Even when the live chunks carry no id/name (announcement + args
        # fragments arrive separately), the finalized record is complete.
        sink = CollectingSink()
        message = FakeMessageWithToolChunks(
            [
                {"index": 0, "id": "", "name": "", "args": '{"target": "e2'},
                {"index": 0, "id": "", "name": "", "args": '3"}'},
            ],
            finalized=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "browser_click",
                    "args": {"target": "e23"},
                }
            ],
        )
        fake_stream = SimpleNamespace(
            messages=async_items([message]),
            tool_calls=async_items([]),
            subagents=async_items([]),
            output=done({}),
        )
        await consume_deepagent_stream(fake_stream, sink=sink, root_source="orchestrator")

        turn = next(e for e in sink.events if e.event == "agent_turn")
        self.assertEqual(len(turn.data["tool_calls"]), 1)
        self.assertEqual(turn.data["tool_calls"][0]["id"], "call-1")
        self.assertEqual(turn.data["tool_calls"][0]["name"], "browser_click")
        self.assertEqual(turn.data["tool_calls"][0]["args"], {"target": "e23"})

    async def test_empty_tool_call_chunks_are_skipped_but_named_ones_kept(self) -> None:
        sink = CollectingSink()
        message = FakeMessageWithToolChunks(
            [
                {"index": 0, "id": "", "name": "", "args": ""},
                {"index": 0, "id": "call-1", "name": "browser_click", "args": ""},
                {"index": 0, "id": "call-1", "name": "browser_click", "args": '{"target": "e23"}'},
            ],
            finalized=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "browser_click",
                    "args": {"target": "e23"},
                }
            ],
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
        self.assertEqual(tool_calls[0]["args"], {"target": "e23"})

    async def test_turn_with_only_empty_chunks_is_dropped_entirely(self) -> None:
        sink = CollectingSink()
        message = FakeMessageWithToolChunks(
            [{"index": 0, "id": "", "name": "", "args": ""}],
            finalized=[],
        )
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
