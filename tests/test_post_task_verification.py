from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from z_apply_core.agents.post_task_verification import (
    PostTaskVerificationMiddleware,
    VerdictState,
    _make_verifier_tools,
)


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


def _make_middleware() -> PostTaskVerificationMiddleware:
    snapshot_tool = AsyncMock(return_value="<snapshot/>")
    snapshot_tool.name = "browser_snapshot"
    with patch(
        "z_apply_core.agents.post_task_verification.create_deep_agent",
        return_value=MagicMock(),
    ):
        mw = PostTaskVerificationMiddleware(
            fallback_model=MagicMock(),
            router=MagicMock(),
            read_only_browser_tools=[snapshot_tool],
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
            result = self._run(
                mw._verify(task_description="click apply", snapshot="<page/>")
            )
        self.assertTrue(result.startswith("verifier_error:"))
        self.assertIn("model down", result)

    def test_verify_returns_verifier_error_when_no_verdict(self) -> None:
        mw = _make_middleware()
        run_output = MagicMock()
        run_output.output = {"messages": [MagicMock(content="")]}

        async def fake_consume(*args: Any, **kwargs: Any) -> Any:
            return run_output

        verifier_instance = MagicMock()
        verifier_instance.astream_events = MagicMock(return_value="stream")
        with patch.object(mw, "_build_verifier", return_value=verifier_instance), patch(
            "z_apply_core.agents.post_task_verification.consume_deepagent_stream",
            side_effect=fake_consume,
        ):
            result = self._run(
                mw._verify(task_description="click apply", snapshot="<page/>")
            )
        self.assertTrue(result.startswith("verifier_error:"))
        self.assertIn("without recording a verdict", result)

    def test_verify_returns_verdict_from_tool(self) -> None:
        mw = _make_middleware()
        captured_state: VerdictState | None = None

        async def fake_consume(*args: Any, **kwargs: Any) -> Any:
            nonlocal captured_state
            captured_state = VerdictState()
            tools = _make_verifier_tools(captured_state)
            verify_tool = next(t for t in tools if t.name == "verification_verified")
            await verify_tool.ainvoke({"evidence": "apply form opened"})
            return MagicMock(output={"messages": []})

        verifier_instance = MagicMock()
        verifier_instance.astream_events = MagicMock(return_value="stream")
        with patch.object(mw, "_build_verifier", return_value=verifier_instance), patch(
            "z_apply_core.agents.post_task_verification.consume_deepagent_stream",
            side_effect=fake_consume,
        ):
            result = self._run(
                mw._verify(task_description="click apply", snapshot="<page/>")
            )
        self.assertIn("verifier_error", result)

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
