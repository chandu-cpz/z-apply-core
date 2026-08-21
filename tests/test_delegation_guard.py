from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import ToolMessage

from z_apply_core.agents.delegation_guard import (
    DelegationFailureLadder,
    DelegationResultMiddleware,
)
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware


def _task_request(subagent_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": "task", "args": {"subagent_type": subagent_type}, "id": "call-1"},
        state={"messages": []},
    )


def _ask_request() -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={
            "name": "ask_human",
            "args": {"reason": "missing_candidate_fact", "field_label": "Expected salary"},
            "id": "call-1",
        },
        state={"messages": []},
    )


class DelegationResultMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_answer_writer_result_becomes_failure_and_counts(self) -> None:
        ladder = DelegationFailureLadder()
        middleware = DelegationResultMiddleware(ladder)
        empty = ToolMessage(content="   ", tool_call_id="call-1")
        handler = AsyncMock(return_value=empty)

        result = await middleware.awrap_tool_call(_task_request("AnswerWriter"), handler)

        self.assertIsInstance(result, ToolMessage)
        assert isinstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("Delegation failed", result.content)
        self.assertIn("no usable output", result.content)
        self.assertEqual(result.tool_call_id, "call-1")
        self.assertEqual(ladder.count, 1)

    async def test_non_empty_result_passes_through_without_counting(self) -> None:
        ladder = DelegationFailureLadder()
        middleware = DelegationResultMiddleware(ladder)
        usable = ToolMessage(content="resolved 2 fields", tool_call_id="call-1")
        handler = AsyncMock(return_value=usable)

        result = await middleware.awrap_tool_call(_task_request("AnswerWriter"), handler)

        self.assertIs(result, usable)
        self.assertEqual(ladder.count, 0)


class EscalationLadderTests(unittest.IsolatedAsyncioTestCase):
    async def test_guard_denies_until_ladder_trips_then_allows(self) -> None:
        ladder = DelegationFailureLadder()
        guard = HumanEscalationGuardMiddleware(
            allowed_reasons=frozenset({"human_challenge"}),
            delegation_ladder=ladder,
        )
        sentinel = ToolMessage(content="answer from human", tool_call_id="call-1")
        handler = AsyncMock(return_value=sentinel)
        request = _ask_request()

        denied = await guard.awrap_tool_call(request, handler)

        self.assertIsInstance(denied, ToolMessage)
        assert isinstance(denied, ToolMessage)
        self.assertIn("Delegate candidate-field questions", denied.content)
        handler.assert_not_awaited()

        ladder.record_failure("AnswerWriter")
        still_denied = await guard.awrap_tool_call(request, handler)
        self.assertIsInstance(still_denied, ToolMessage)
        handler.assert_not_awaited()
        self.assertFalse(ladder.tripped)

        ladder.record_failure("AnswerWriter")
        allowed = await guard.awrap_tool_call(request, handler)

        self.assertIs(allowed, sentinel)
        handler.assert_awaited_once_with(request)


if __name__ == "__main__":
    unittest.main()
