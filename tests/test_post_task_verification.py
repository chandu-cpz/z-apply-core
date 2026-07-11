from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from z_apply_core.agents.post_task_verification import PostTaskVerificationMiddleware
from z_apply_core.stream_events import FrameworkTraceEvent


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[FrameworkTraceEvent] = []

    async def accept(self, event: FrameworkTraceEvent) -> None:
        self.events.append(event)


def _task_request(subagent_type: str = "BrowserSpecialist") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": "task",
            "id": "call-1",
            "args": {
                "description": "Open the application form",
                "subagent_type": subagent_type,
            },
        },
        tool=MagicMock(),
        state={},
        runtime=MagicMock(),
    )


def _task_result(content: str) -> Command[Any]:
    return Command(
        update={
            "messages": [ToolMessage(content=content, tool_call_id="call-1")],
        }
    )


def _make_middleware(
    sink: CollectingSink | None = None,
) -> PostTaskVerificationMiddleware:
    snapshot_tool = AsyncMock()
    snapshot_tool.ainvoke = AsyncMock(return_value="<fresh form snapshot/>")
    snapshot_tool.name = "browser_snapshot"
    return PostTaskVerificationMiddleware(
        read_only_browser_tools=[snapshot_tool],
        sink=sink,
    )


class NativeTaskPairingTests(unittest.TestCase):
    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_browser_task_is_followed_by_native_verifier_task(self) -> None:
        middleware = _make_middleware()
        calls: list[dict[str, Any]] = []

        async def handler(request: ToolCallRequest) -> Command[Any]:
            arguments = request.tool_call["args"]
            calls.append(arguments)
            if arguments["subagent_type"] == "BrowserSpecialist":
                return _task_result("Clicked Apply and observed the form")
            return _task_result("verified: Fresh evidence shows application fields")

        result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertEqual(
            [call["subagent_type"] for call in calls],
            ["BrowserSpecialist", "Verifier"],
        )
        verifier_prompt = calls[1]["description"]
        self.assertIn("Open the application form", verifier_prompt)
        self.assertIn("Clicked Apply and observed the form", verifier_prompt)
        self.assertIn("<fresh form snapshot/>", verifier_prompt)
        message = result.update["messages"][0]
        self.assertIn("BROWSER_SPECIALIST_RESULT", str(message.content))
        self.assertIn("VERIFIER_RESULT", str(message.content))
        self.assertIn("verified: Fresh evidence", str(message.content))

    def test_non_browser_task_uses_native_handler_once(self) -> None:
        middleware = _make_middleware()
        handler = AsyncMock(return_value=_task_result("mapped fields"))

        result = self._run(
            middleware.awrap_tool_call(_task_request("FieldMapper"), handler)
        )

        self.assertEqual(handler.await_count, 1)
        self.assertEqual(result, handler.return_value)

    def test_verifier_failure_is_returned_without_repeating_browser_task(self) -> None:
        middleware = _make_middleware()
        calls: list[str] = []

        async def handler(request: ToolCallRequest) -> Command[Any]:
            subagent_type = request.tool_call["args"]["subagent_type"]
            calls.append(subagent_type)
            if subagent_type == "Verifier":
                raise RuntimeError("model unavailable")
            return _task_result("Browser mutation completed")

        result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertEqual(calls, ["BrowserSpecialist", "Verifier"])
        message = result.update["messages"][0]
        self.assertIn("VERIFIER_ERROR: model unavailable", str(message.content))

    def test_browser_result_without_tool_message_skips_verifier(self) -> None:
        middleware = _make_middleware()
        handler = AsyncMock(return_value=Command(update={"messages": []}))

        result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertEqual(handler.await_count, 1)
        self.assertEqual(result, handler.return_value)

    def test_fresh_snapshot_is_visible_to_stream_sink(self) -> None:
        sink = CollectingSink()
        middleware = _make_middleware(sink)

        snapshot = self._run(middleware._fresh_snapshot())

        self.assertEqual(snapshot, "<fresh form snapshot/>")
        self.assertEqual(
            [event.event for event in sink.events],
            ["agent_tool_start", "agent_tool_end"],
        )
        self.assertTrue(sink.events[-1].data["completed"])


if __name__ == "__main__":
    unittest.main()
