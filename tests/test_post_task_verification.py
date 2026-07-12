from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from z_apply_core.agents.application_progress import ApplicationProgress
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

    mock_model = MagicMock()
    mock_model.bind_tools = MagicMock(return_value=mock_model)

    mock_router = MagicMock()
    mock_router.lease = AsyncMock(
        return_value=SimpleNamespace(
            info=SimpleNamespace(id="test-model"),
            llm=mock_model,
            callback=MagicMock(),
        )
    )

    return PostTaskVerificationMiddleware(
        fallback_model=mock_model,
        router=mock_router,
        read_only_browser_tools=[snapshot_tool],
        progress=ApplicationProgress(),
        sink=sink,
    )


class NativeTaskPairingTests(unittest.TestCase):
    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    @patch("z_apply_core.agents.post_task_verification.create_deep_agent")
    def test_browser_task_is_followed_by_native_verifier_task(self, mock_create: MagicMock) -> None:
        mock_graph = AsyncMock()
        mock_graph.astream_events = AsyncMock(return_value=iter([]))
        mock_create.return_value = mock_graph

        middleware = _make_middleware()
        calls: list[dict[str, Any]] = []

        async def handler(request: ToolCallRequest) -> Command[Any]:
            arguments = request.tool_call["args"]
            calls.append(arguments)
            if arguments["subagent_type"] == "BrowserSpecialist":
                return _task_result("Clicked Apply and observed the form")
            return _task_result("verified: Fresh evidence shows application fields")

        async def fake_verify(**kwargs: Any) -> Any:
            from z_apply_core.agents.post_task_verification import VerificationDecision

            return VerificationDecision(
                operation="form_open",
                status="verified",
                evidence="Fresh snapshot confirms form is open",
            )

        with patch.object(middleware, "_verify", side_effect=fake_verify):
            result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertEqual(
            [call["subagent_type"] for call in calls],
            ["BrowserSpecialist"],
        )
        message = result.update["messages"][0]
        self.assertIn("BROWSER_SPECIALIST_RESULT", str(message.content))
        self.assertIn("VERIFIER_RESULT", str(message.content))
        self.assertIn("verified", str(message.content))

    def test_non_browser_task_uses_native_handler_once(self) -> None:
        middleware = _make_middleware()
        handler = AsyncMock(return_value=_task_result("mapped fields"))

        result = self._run(middleware.awrap_tool_call(_task_request("FieldMapper"), handler))

        self.assertEqual(handler.await_count, 1)
        self.assertEqual(result, handler.return_value)

    def test_non_browser_specialist_timeout_returns_typed_error(self) -> None:
        middleware = _make_middleware()

        async def handler(_request: ToolCallRequest) -> Command[Any]:
            raise RuntimeError("field snapshot connection closed")

        result = self._run(middleware.awrap_tool_call(_task_request("FieldMapper"), handler))

        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("field snapshot connection closed", str(result.content))

    def test_verifier_failure_is_returned_as_typed_error(self) -> None:
        middleware = _make_middleware()
        calls: list[str] = []

        async def handler(request: ToolCallRequest) -> Command[Any]:
            subagent_type = request.tool_call["args"]["subagent_type"]
            calls.append(subagent_type)
            return _task_result("Browser mutation completed")

        async def failing_verify(**kwargs: Any) -> None:
            return None

        with patch.object(middleware, "_verify", side_effect=failing_verify):
            result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertEqual(calls, ["BrowserSpecialist"])
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("no typed verdict", str(result.content))

    def test_browser_result_without_tool_message_skips_verifier(self) -> None:
        middleware = _make_middleware()
        handler = AsyncMock(return_value=Command(update={"messages": []}))

        result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertEqual(handler.await_count, 1)
        self.assertEqual(result, handler.return_value)

    def test_browser_task_timeout_returns_typed_error_to_orchestrator(self) -> None:
        middleware = _make_middleware()

        async def handler(_request: ToolCallRequest) -> Command[Any]:
            raise TimeoutError("model stream timed out")

        result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("model stream timed out", str(result.content))

    def test_typed_browser_failure_returns_typed_error(self) -> None:
        middleware = _make_middleware()
        calls: list[dict[str, Any]] = []

        async def handler(request: ToolCallRequest) -> Command[Any]:
            arguments = request.tool_call["args"]
            calls.append(arguments)
            if arguments["subagent_type"] == "BrowserSpecialist":
                raise RuntimeError("browser_snapshot does not handle the modal state")
            return _task_result("verified uploaded resume")

        result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertEqual(
            [call["subagent_type"] for call in calls],
            ["BrowserSpecialist"],
        )
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("browser_snapshot does not handle the modal state", str(result.content))

    def test_fresh_snapshot_is_visible_to_stream_sink(self) -> None:
        sink = CollectingSink()
        middleware = _make_middleware(sink)

        snapshot = self._run(middleware._fresh_snapshot())

        self.assertEqual(snapshot.content, "<fresh form snapshot/>")
        self.assertTrue(snapshot.collected)
        self.assertEqual(
            [event.event for event in sink.events],
            ["agent_tool_start", "agent_tool_end"],
        )
        self.assertTrue(sink.events[-1].data["completed"])

    @patch("z_apply_core.agents.post_task_verification.create_deep_agent")
    def test_snapshot_failure_returns_typed_error(self, mock_create: MagicMock) -> None:
        snapshot_tool = AsyncMock()
        snapshot_tool.ainvoke = AsyncMock(side_effect=RuntimeError("browser evidence unavailable"))
        snapshot_tool.name = "browser_snapshot"

        mock_model = MagicMock()
        mock_model.bind_tools = MagicMock(return_value=mock_model)

        mock_router = MagicMock()
        mock_router.lease = AsyncMock(
            return_value=SimpleNamespace(
                info=SimpleNamespace(id="test-model"),
                llm=mock_model,
                callback=MagicMock(),
            )
        )

        middleware = PostTaskVerificationMiddleware(
            fallback_model=mock_model,
            router=mock_router,
            read_only_browser_tools=[snapshot_tool],
            progress=ApplicationProgress(),
        )
        calls: list[dict[str, Any]] = []

        async def handler(request: ToolCallRequest) -> Command[Any]:
            arguments = request.tool_call["args"]
            calls.append(arguments)
            return _task_result("browser attempt completed")

        result = self._run(middleware.awrap_tool_call(_task_request(), handler))

        self.assertEqual(
            [call["subagent_type"] for call in calls],
            ["BrowserSpecialist"],
        )
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("browser evidence unavailable", str(result.content))


if __name__ == "__main__":
    unittest.main()
