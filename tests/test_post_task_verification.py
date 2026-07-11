from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from z_apply_core.agents.post_task_verification import (
    PostTaskVerificationMiddleware,
    _last_message_text,
)


class LastMessageTextTests(unittest.TestCase):
    def test_string_content(self) -> None:
        messages = [MagicMock(content="hello world")]
        self.assertEqual(_last_message_text({"messages": messages}), "hello world")

    def test_list_content(self) -> None:
        messages = [MagicMock(content=[{"text": "item1"}, {"text": "item2"}])]
        self.assertEqual(_last_message_text({"messages": messages}), "item1\nitem2")

    def test_empty_messages(self) -> None:
        self.assertEqual(_last_message_text({"messages": []}), "")

    def test_non_list_messages(self) -> None:
        self.assertEqual(_last_message_text({"messages": "bad"}), "")

    def test_whitespace_only_returns_empty(self) -> None:
        messages = [MagicMock(content="   ")]
        self.assertEqual(_last_message_text({"messages": messages}), "")


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
        mw._verifier = MagicMock()
        mw._verifier.astream_events = AsyncMock(side_effect=RuntimeError("model down"))
        result = self._run(
            mw._verify(task_description="click apply", snapshot="<page/>")
        )
        self.assertTrue(result.startswith("verifier_error:"))
        self.assertIn("model down", result)

    def test_verify_returns_verifier_error_when_empty(self) -> None:
        mw = _make_middleware()
        run_output = MagicMock()
        run_output.output = {"messages": [MagicMock(content="")]}

        async def fake_astream(*args: Any, **kwargs: Any) -> Any:
            return run_output

        mw._verifier = MagicMock()
        mw._verifier.astream_events = fake_astream

        with patch(
            "z_apply_core.agents.post_task_verification.consume_deepagent_stream",
            new_callable=AsyncMock,
            return_value=run_output,
        ):
            result = self._run(
                mw._verify(task_description="click apply", snapshot="<page/>")
            )
        self.assertTrue(result.startswith("verifier_error:"))

    def test_verify_returns_not_verified_with_content(self) -> None:
        mw = _make_middleware()
        run_output = MagicMock()
        run_output.output = {
            "messages": [MagicMock(content="verified: apply button was clicked")]
        }

        async def fake_astream(*args: Any, **kwargs: Any) -> Any:
            return run_output

        mw._verifier = MagicMock()
        mw._verifier.astream_events = fake_astream

        with patch(
            "z_apply_core.agents.post_task_verification.consume_deepagent_stream",
            new_callable=AsyncMock,
            return_value=run_output,
        ):
            result = self._run(
                mw._verify(task_description="click apply", snapshot="<page/>")
            )
        self.assertEqual(result, "verified: apply button was clicked")

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
