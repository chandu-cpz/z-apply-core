from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from z_apply_core.agents.result import OrchestratorRun
from z_apply_core.browser_tools import AUTH_AGENT_BROWSER_TOOLS
from z_apply_core.nodes.authenticate_default_account import (
    SIMPLIFY_DASHBOARD_URL,
    authenticate_default_account,
)
from z_apply_core.runtime import RunRuntime


class FakeTools:
    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.langchain_tool_requests: list[tuple[str, ...]] = []

    async def call(self, name: str, arguments: dict[str, object] | None = None) -> str:
        self.calls.append((name, arguments or {}))
        values = self.responses.get(name)
        if values:
            return values.pop(0)
        return ""

    def langchain_tools(self, names: tuple[str, ...]) -> list[Any]:
        self.langchain_tool_requests.append(names)
        return [SimpleNamespace(name=name) for name in names]


class FakeHumanChannel:
    async def ask(self, **_kwargs: object) -> str:
        return "Done"

    async def confirm(self, **_kwargs: object) -> bool:
        return True


def make_runtime(tools: FakeTools, human_channel: object | None = None) -> RunRuntime:
    return RunRuntime(
        display=SimpleNamespace(stop=lambda: None),  # type: ignore[arg-type]
        live_view=SimpleNamespace(stop=lambda: None),  # type: ignore[arg-type]
        browser=SimpleNamespace(tools=tools, close=lambda: None),  # type: ignore[arg-type]
        human_channel=human_channel,  # type: ignore[arg-type]
    )


class AuthenticateDefaultAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_node_delegates_to_auth_orchestrator_and_restores_job_page(self) -> None:
        tools = FakeTools(
            {
                "browser_navigate": [
                    "- Page URL: https://simplify.jobs/dashboard\n- text: Login",
                    "- heading Job details",
                ],
                "browser_snapshot": [
                    "- Page URL: https://simplify.jobs/dashboard\n- text: Login",
                    "- heading Job details",
                ],
            }
        )
        runtime = make_runtime(tools, FakeHumanChannel())
        settings = SimpleNamespace(
            has_default_credentials=True,
            default_username="user@example.test",
            default_password="secret",
        )
        captured: dict[str, Any] = {}

        async def fake_run_auth_orchestrator(**kwargs: Any) -> OrchestratorRun:
            captured.update(kwargs)
            return OrchestratorRun(
                summary="authenticated: Simplify verified by Verifier.",
                model_id="test/model",
            )

        with (
            patch(
                "z_apply_core.nodes.authenticate_default_account.load_settings",
                return_value=settings,
            ),
            patch(
                "z_apply_core.nodes.authenticate_default_account.run_auth_orchestrator",
                side_effect=fake_run_auth_orchestrator,
            ),
        ):
            result = await authenticate_default_account(
                {"runtime": runtime, "job_url": "https://jobs.example/job/1"},
                {},
            )

        self.assertEqual(result["auth_status"], "authenticated")
        self.assertEqual(result["auth_model_id"], "test/model")
        self.assertEqual(
            tools.calls,
            [
                ("browser_navigate", {"url": SIMPLIFY_DASHBOARD_URL}),
                ("browser_snapshot", {}),
                ("browser_navigate", {"url": "https://jobs.example/job/1"}),
                ("browser_snapshot", {}),
            ],
        )
        self.assertEqual(tools.langchain_tool_requests, [AUTH_AGENT_BROWSER_TOOLS])
        self.assertEqual(
            [tool.name for tool in captured["browser_tools"]],
            list(AUTH_AGENT_BROWSER_TOOLS),
        )
        self.assertEqual(len(captured["human_tools"]), 2)

    async def test_auth_node_skips_when_default_credentials_are_missing(self) -> None:
        tools = FakeTools({})
        runtime = make_runtime(tools)
        settings = SimpleNamespace(
            has_default_credentials=False,
            default_username="",
            default_password="",
        )

        with patch(
            "z_apply_core.nodes.authenticate_default_account.load_settings",
            return_value=settings,
        ):
            result = await authenticate_default_account(
                {"runtime": runtime, "job_url": "https://jobs.example/job/1"},
                {},
            )

        self.assertEqual(result["auth_status"], "skipped")
        self.assertEqual(tools.calls, [])


if __name__ == "__main__":
    unittest.main()
