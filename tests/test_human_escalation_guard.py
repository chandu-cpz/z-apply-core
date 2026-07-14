from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import ToolMessage

from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware


def _request(args: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": "ask_human", "args": args, "id": "call-1"})


class HumanEscalationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_candidate_question_without_exact_field(self) -> None:
        handler = AsyncMock()
        result = await HumanEscalationGuardMiddleware().awrap_tool_call(
            _request({"reason": "missing_candidate_fact"}), handler
        )

        self.assertIsInstance(result, ToolMessage)
        handler.assert_not_awaited()

    async def test_allows_specific_candidate_question(self) -> None:
        expected = ToolMessage(content="answer", tool_call_id="call-1")
        handler = AsyncMock(return_value=expected)
        request = _request({"reason": "missing_candidate_fact", "field_label": "Expected salary"})

        result = await HumanEscalationGuardMiddleware().awrap_tool_call(request, handler)

        self.assertIs(result, expected)
        handler.assert_awaited_once_with(request)

    async def test_allows_concrete_human_challenge_without_field_label(self) -> None:
        expected = ToolMessage(content="done", tool_call_id="call-1")
        handler = AsyncMock(return_value=expected)
        request = _request({"reason": "human_challenge"})

        result = await HumanEscalationGuardMiddleware().awrap_tool_call(request, handler)

        self.assertIs(result, expected)

    async def test_orchestrator_guard_denies_candidate_questions(self) -> None:
        handler = AsyncMock()
        guard = HumanEscalationGuardMiddleware(allowed_reasons=frozenset({"human_challenge"}))

        result = await guard.awrap_tool_call(
            _request({"reason": "missing_candidate_fact", "field_label": "Expected salary"}),
            handler,
        )

        self.assertIsInstance(result, ToolMessage)
        handler.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
