from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from z_apply_core.agents.post_task_verification import (
    MAX_VERDICT_ATTEMPTS,
    PostTaskVerificationMiddleware,
    VerdictState,
    _make_verifier_tools,
)
from z_apply_core.stream_events import FrameworkTraceEvent


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[FrameworkTraceEvent] = []

    async def accept(self, event: FrameworkTraceEvent) -> None:
        self.events.append(event)


class VerifierToolTests(unittest.TestCase):
    def _run(self, coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_verified_tool_records_decision(self) -> None:
        state = VerdictState()
        tools = _make_verifier_tools(state)
        self.assertIsNone(state.decision)
        verify_tool = next(t for t in tools if t.name == "verification_verified")
        result = self._run(verify_tool.ainvoke({"evidence": "apply button opened form"}))
        self.assertIn("recorded", result)
        self.assertIsNotNone(state.decision)
        self.assertEqual(state.decision.status, "verified")
        self.assertEqual(state.decision.detail, "apply button opened form")

    def test_not_verified_tool_records_decision(self) -> None:
        state = VerdictState()
        tools = _make_verifier_tools(state)
        verify_tool = next(t for t in tools if t.name == "verification_not_verified")
        result = self._run(verify_tool.ainvoke({"reason": "form not visible"}))
        self.assertIn("recorded", result)
        self.assertIsNotNone(state.decision)
        self.assertEqual(state.decision.status, "not_verified")
        self.assertEqual(state.decision.detail, "form not visible")

    def test_blocked_tool_records_decision(self) -> None:
        state = VerdictState()
        tools = _make_verifier_tools(state)
        verify_tool = next(t for t in tools if t.name == "verification_blocked")
        result = self._run(verify_tool.ainvoke({"reason": "CAPTCHA blocks submit"}))
        self.assertIn("recorded", result)
        self.assertIsNotNone(state.decision)
        self.assertEqual(state.decision.status, "blocked")
        self.assertEqual(state.decision.detail, "CAPTCHA blocks submit")

    def test_second_tool_call_ignored(self) -> None:
        state = VerdictState()
        tools = _make_verifier_tools(state)
        verify_tool = next(t for t in tools if t.name == "verification_verified")
        self._run(verify_tool.ainvoke({"evidence": "first call"}))
        result = self._run(verify_tool.ainvoke({"evidence": "second call"}))
        self.assertIn("recorded", result)
        self.assertEqual(state.decision.detail, "first call")

    def test_tools_list_has_three_verdict_tools(self) -> None:
        tools = _make_verifier_tools()
        names = {t.name for t in tools}
        self.assertEqual(
            names,
            {"verification_verified", "verification_not_verified", "verification_blocked"},
        )


def _make_middleware(
    sink: CollectingSink | None = None,
) -> PostTaskVerificationMiddleware:
    snapshot_tool = AsyncMock(return_value="<snapshot/>")
    snapshot_tool.ainvoke = AsyncMock(return_value="<snapshot/>")
    snapshot_tool.name = "browser_snapshot"
    with patch(
        "z_apply_core.agents.post_task_verification.create_deep_agent",
        return_value=MagicMock(),
    ):
        mw = PostTaskVerificationMiddleware(
            fallback_model=MagicMock(),
            router=MagicMock(),
            read_only_browser_tools=[snapshot_tool],
            sink=sink,
        )
    return mw


class VerifyResultTests(unittest.TestCase):
    def _run(self, coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_verify_returns_verifier_error_on_exception(self) -> None:
        mw = _make_middleware()
        verifier_instance = MagicMock()
        verifier_instance.astream_events = MagicMock(side_effect=RuntimeError("model down"))
        with patch.object(mw, "_build_verifier", return_value=verifier_instance):
            result = self._run(mw._verify(task_description="click apply", snapshot="<page/>"))
        self.assertTrue(result.startswith("not_verified:"))
        self.assertIn("model down", result)

    def test_verify_returns_verifier_error_when_no_verdict(self) -> None:
        mw = _make_middleware()
        run_output = MagicMock()
        run_output.output = {"messages": [MagicMock(content="")]}

        async def fake_consume(*args: Any, **kwargs: Any) -> Any:
            return run_output

        verifier_instance = MagicMock()
        verifier_instance.astream_events = MagicMock(return_value="stream")
        with (
            patch.object(mw, "_build_verifier", return_value=verifier_instance),
            patch(
                "z_apply_core.agents.post_task_verification.consume_deepagent_stream",
                side_effect=fake_consume,
            ) as consume,
        ):
            result = self._run(mw._verify(task_description="click apply", snapshot="<page/>"))
        self.assertTrue(result.startswith("not_verified:"))
        self.assertIn("did not record a verdict", result)
        self.assertEqual(consume.await_count, MAX_VERDICT_ATTEMPTS)

    def test_verify_returns_verdict_from_tool_and_forwards_sink(self) -> None:
        sink = CollectingSink()
        mw = _make_middleware(sink)
        captured_tools: list[list[Any]] = []

        async def fake_consume(*args: Any, **kwargs: Any) -> Any:
            tools = captured_tools[-1]
            verify_tool = next(tool for tool in tools if tool.name == "verification_verified")
            await verify_tool.ainvoke({"evidence": "apply form opened"})
            return MagicMock(output={"messages": []})

        verifier_instance = MagicMock()
        verifier_instance.astream_events = MagicMock(return_value="stream")
        with (
            patch.object(
                mw,
                "_build_verifier",
                side_effect=lambda tools: captured_tools.append(tools) or verifier_instance,
            ),
            patch(
                "z_apply_core.agents.post_task_verification.consume_deepagent_stream",
                side_effect=fake_consume,
            ) as consume,
        ):
            result = self._run(mw._verify(task_description="click apply", snapshot="<page/>"))
        self.assertEqual(result, "verified: apply form opened")
        self.assertEqual(consume.await_count, 1)
        self.assertIs(consume.await_args.kwargs["sink"], sink)
        self.assertEqual(consume.await_args.kwargs["root_source"], "PostTaskVerifier")

    def test_fresh_snapshot_is_visible_to_the_sink(self) -> None:
        sink = CollectingSink()
        mw = _make_middleware(sink)

        snapshot = self._run(mw._fresh_snapshot())

        self.assertEqual(snapshot, "<snapshot/>")
        self.assertEqual(
            [event.event for event in sink.events],
            ["agent_tool_start", "agent_tool_end"],
        )
        self.assertTrue(sink.events[-1].data["completed"])

    def test_verify_returns_verifier_error_on_snapshot_failure(self) -> None:
        mw = _make_middleware()
        snapshot_tool = AsyncMock()
        snapshot_tool.ainvoke = AsyncMock(side_effect=RuntimeError("browser crashed"))
        snapshot_tool.name = "browser_snapshot"
        mw._snapshot_tool = snapshot_tool
        snapshot = self._run(mw._fresh_snapshot())
        self.assertTrue(snapshot.startswith("Snapshot unavailable:"))
        self.assertIn("browser crashed", snapshot)


if __name__ == "__main__":
    unittest.main()
