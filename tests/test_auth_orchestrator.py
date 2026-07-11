from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from nim_router import NimRouter

from z_apply_core.agents.auth_orchestrator import (
    MAX_AUTH_VERDICT_ATTEMPTS,
    run_auth_orchestrator,
)
from z_apply_core.stream_events import V3RunResult


class FakeAuthAgent:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def astream_events(self, value: dict[str, Any], **_kwargs: Any) -> object:
        self.inputs.append(value)
        return object()


class AuthOrchestratorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_auth_verdict_is_retried_then_not_verified(self) -> None:
        router = NimRouter()
        selection = SimpleNamespace(llm=MagicMock(), info=SimpleNamespace(id="provider/model"))
        agent = FakeAuthAgent()

        with (
            patch.object(router, "lease", AsyncMock(return_value=selection)),
            patch(
                "z_apply_core.agents.auth_orchestrator.create_deep_agent",
                return_value=agent,
            ),
            patch(
                "z_apply_core.agents.auth_orchestrator.build_auth_specialists",
                AsyncMock(return_value=[]),
            ),
            patch(
                "z_apply_core.agents.auth_orchestrator.consume_deepagent_stream",
                AsyncMock(return_value=V3RunResult(output={})),
            ) as consume,
        ):
            result = await run_auth_orchestrator(
                snapshot="Simplify dashboard",
                browser_tools=[],
                human_tools=[],
                config={},
                router=router,
            )

        self.assertEqual(result.status, "not_verified")
        self.assertIn("did not record", result.summary)
        self.assertEqual(len(agent.inputs), MAX_AUTH_VERDICT_ATTEMPTS)
        self.assertEqual(consume.await_count, MAX_AUTH_VERDICT_ATTEMPTS)

    async def test_auth_verdict_after_retry_requires_fresh_specialist_inspection(self) -> None:
        router = NimRouter()
        selection = SimpleNamespace(llm=MagicMock(), info=SimpleNamespace(id="provider/model"))
        agent = FakeAuthAgent()
        captured_tools: list[Any] = []

        def make_agent(**kwargs: Any) -> FakeAuthAgent:
            captured_tools.extend(kwargs["tools"])
            return agent

        calls = 0

        async def stream_result(*_args: Any, **_kwargs: Any) -> V3RunResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return V3RunResult(
                    output={
                        "messages": [],
                        "_z_apply_tool_trace": [
                            {
                                "source": "BrowserSpecialist",
                                "tool_name": "browser_snapshot",
                                "completed": True,
                                "error": "",
                            }
                        ]
                    }
                )
            verdict = next(
                tool for tool in captured_tools if tool.name == "authentication_verified"
            )
            await verdict.ainvoke({"evidence": "Welcome, Chandrakanth"})
            return V3RunResult(output={"messages": []})

        with (
            patch.object(router, "lease", AsyncMock(return_value=selection)),
            patch(
                "z_apply_core.agents.auth_orchestrator.create_deep_agent",
                side_effect=make_agent,
            ),
            patch(
                "z_apply_core.agents.auth_orchestrator.build_auth_specialists",
                AsyncMock(return_value=[]),
            ),
            patch(
                "z_apply_core.agents.auth_orchestrator.consume_deepagent_stream",
                side_effect=stream_result,
            ),
        ):
            result = await run_auth_orchestrator(
                snapshot="Simplify dashboard",
                browser_tools=[],
                human_tools=[],
                config={},
                router=router,
            )

        self.assertEqual(result.status, "authenticated")
        self.assertEqual(result.summary, "Welcome, Chandrakanth")
        self.assertEqual(len(agent.inputs), 2)
        reminder = agent.inputs[1]["messages"][-1]
        self.assertIn("call exactly one authentication verdict tool", reminder.content)


if __name__ == "__main__":
    unittest.main()
