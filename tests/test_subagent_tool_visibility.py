from __future__ import annotations

import unittest
from collections.abc import Iterator, Sequence

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from z_apply_core.agents.deepagent_stream import consume_deepagent_stream
from z_apply_core.stream_events import FrameworkTraceEvent


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[FrameworkTraceEvent] = []

    async def accept(self, event: FrameworkTraceEvent) -> None:
        self.events.append(event)


class ProbeToolInput(BaseModel):
    query: str = Field(default="probe")


class ProbeTool(BaseTool):
    name: str = "probe_tool"
    description: str = "probe tool used by the AnswerWriter subagent"
    args_schema: type[ProbeToolInput] = ProbeToolInput

    def _run(self, query: str = "probe") -> str:
        return "probe result"

    async def _arun(self, query: str = "probe") -> str:
        return "probe result"


class AsyncFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel without an async generate path; deep agent graphs
    invoke the model node asynchronously, so delegate ``_agenerate`` to the
    sync implementation."""

    async def _agenerate(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> object:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]

    def _stream(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> Iterator[ChatGenerationChunk]:
        message = next(self.messages)
        message_ = AIMessage(content=message) if isinstance(message, str) else message
        chunk = AIMessageChunk(
            content=message_.content,
            tool_calls=message_.tool_calls,
        )
        chunk.chunk_position = "last"
        if run_manager is not None:
            run_manager.on_llm_new_token(str(message_.content), chunk=chunk)
        yield ChatGenerationChunk(message=chunk)

    def bind_tools(
        self,
        tools: Sequence[object],
        *,
        tool_choice: str | None = None,
        **kwargs: object,
    ) -> object:
        return self


class SubagentToolVisibilityProbe(unittest.IsolatedAsyncioTestCase):
    """Probe: does the SubagentTransformer handle expose a working
    ``tool_calls`` projection, so subagent tool activity streams as
    ``agent_tool_start``/``agent_tool_end`` events named after the subagent?

    Mirrors the runtime shape: an orchestrator deep agent with one
    declarative AnswerWriter subagent. The fake models dispatch exactly one
    subagent task and one subagent tool call, so the stream must carry the
    subagent's tool activity for the panels to exist.
    """

    async def _collect(self) -> list[FrameworkTraceEvent]:
        orchestrator_model = AsyncFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {
                                    "subagent_type": "AnswerWriter",
                                    "description": "resolve the field",
                                },
                                "id": "task-1",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="resolved everything"),
                ]
            )
        )
        answer_writer_model = AsyncFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "probe_tool",
                                "args": {"query": "x"},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="answer: resolved"),
                ]
            )
        )
        agent = create_deep_agent(
            model=orchestrator_model,
            tools=[ProbeTool()],
            subagents=[
                {
                    "name": "AnswerWriter",
                    "description": "Resolves candidate fields.",
                    "system_prompt": "You resolve candidate fields.",
                    "model": answer_writer_model,
                    "tools": [ProbeTool()],
                }
            ],
            name="orchestrator",
        )
        sink = CollectingSink()
        await consume_deepagent_stream(
            agent.astream_events(
                {"messages": [{"role": "user", "content": "go"}]},
                config={},
                version="v3",
            ),
            sink=sink,
        )
        return sink.events

    async def test_subagent_lifecycle_and_messages_stream(self) -> None:
        events = await self._collect()
        names = {event.name for event in events}
        self.assertIn("AnswerWriter", names)
        lifecycles = [
            event.data.get("status")
            for event in events
            if event.event == "agent_lifecycle" and event.name == "AnswerWriter"
        ]
        self.assertIn("started", lifecycles)
        self.assertIn("completed", lifecycles)
        text_deltas = [
            event.data.get("delta")
            for event in events
            if event.event == "agent_message_delta"
            and event.name == "AnswerWriter"
            and event.data.get("kind") == "text"
        ]
        self.assertTrue(any("answer: resolved" in delta for delta in text_deltas))

    async def test_subagent_tool_calls_projection_yields(self) -> None:
        events = await self._collect()
        starts = [
            event
            for event in events
            if event.event == "agent_tool_start" and event.name == "AnswerWriter"
        ]
        ends = [
            event
            for event in events
            if event.event == "agent_tool_end" and event.name == "AnswerWriter"
        ]
        self.assertEqual(len(starts), 1, [e.event for e in events])
        self.assertEqual(starts[0].data.get("tool_name"), "probe_tool")
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].data.get("tool_name"), "probe_tool")
        self.assertIn("probe result", str(ends[0].data.get("output")))


if __name__ == "__main__":
    unittest.main()
