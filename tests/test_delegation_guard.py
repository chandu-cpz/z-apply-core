from __future__ import annotations

import asyncio
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


class ProductionEscalationStackTests(unittest.TestCase):
    """Wire DEC-002 through the SAME construction path run_orchestrator uses."""

    def test_orchestrator_guard_opens_only_after_production_wiring(self):
        from z_apply_core.agents.orchestrator import build_escalation_stack

        orch_guard, aw_guard, result_mw = build_escalation_stack()
        # One shared ladder across all three gates.
        self.assertIs(orch_guard._ladder, result_mw._ladder)
        self.assertIs(aw_guard._ladder, result_mw._ladder)

        async def run_flow():
            ask = _ask_request()

            # Fresh ladder: direct asking is denied even though the reason is
            # a legitimate candidate fact.
            denied = await orch_guard.awrap_tool_call(ask, AsyncMock())
            self.assertIn("denied", denied.content)

            # Two empty AnswerWriter delegations trip the shared ladder...
            empty = ToolMessage(content="", tool_call_id="t1", name="task")
            task_request = SimpleNamespace(
                tool_call={
                    "name": "task",
                    "id": "t1",
                    "args": {"subagent_type": "AnswerWriter", "description": "x"},
                }
            )
            for _ in range(2):
                out = await result_mw.awrap_tool_call(task_request, AsyncMock(return_value=empty))
                self.assertIn("Delegation failed", out.content)

            # ...and the ORCHESTRATOR's guard now permits the ask.
            sentinel = ToolMessage(content="ok", tool_call_id="c")
            return await orch_guard.awrap_tool_call(ask, AsyncMock(return_value=sentinel))

        allowed = asyncio.run(run_flow())
        self.assertEqual(allowed.content, "ok")

    def test_delegation_result_wraps_dispatch_passthrough(self):
        """Order [SubagentDispatch, DelegationResult] still lets the result
        middleware see the FINAL ToolMessage for AnswerWriter dispatches."""
        import asyncio

        from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware

        _, _, result_mw = build_stack_helper()
        dispatch = SubagentDispatchMiddleware(["AnswerWriter"])
        final = ToolMessage(content="", tool_call_id="t9", name="task")
        inner = AsyncMock(return_value=final)

        async def outer(req):
            return await result_mw.awrap_tool_call(req, inner)

        request = SimpleNamespace(
            tool_call={
                "name": "task",
                "id": "t9",
                "args": {"subagent_type": "AnswerWriter", "description": "x"},
            }
        )
        out = asyncio.run(dispatch.awrap_tool_call(request, outer))
        self.assertIn("Delegation failed", out.content)
        self.assertEqual(out.status, "error")
        inner.assert_awaited_once()


def build_stack_helper():
    from z_apply_core.agents.orchestrator import build_escalation_stack

    return build_escalation_stack()


if __name__ == "__main__":
    unittest.main()
