from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from nim_router import NimRouter

from z_apply_core.agents.authentication import (
    AUTHENTICATION_BROWSER_TOOLS,
    AuthenticationRun,
)
from z_apply_core.browser_tools import AUTHENTICATION_SPECIALIST_BROWSER_TOOLS
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
        browser=SimpleNamespace(  # type: ignore[arg-type]
            tools=tools,
            close=lambda: None,
            submit_auth_form=lambda target: "submitted",
            open_verification_link=lambda url: "verified",
            run_id="test-run",
        ),
        human_channel=human_channel,  # type: ignore[arg-type]
    )


LOGIN_PAGE = "\n".join(
    [
        "- document [ref=e1]:",
        "  - main [ref=e4]:",
        "    - heading \"Login to your account\" [level=1] [ref=e40]",
        "    - textbox \"Email Address\" [ref=e70]:",
        "      - /placeholder: Email Address",
        "    - textbox \"Password\" [ref=e78]",
        "    - iframe [ref=e81]:",
        "      - text: protected by",
        "    - button \"Sign in\" [ref=e92] [cursor=pointer]",
    ]
)

JOB_PAGE = "\n".join(
    [
        "- document [ref=j1]:",
        "  - main [ref=j4]:",
        "    - heading \"Associate-Software-Developer\" [level=1] [ref=j40]",
        "    - button \"Apply\" [ref=j92] [cursor=pointer]",
    ]
)

WELCOME_PAGE = "\n".join(
    [
        "- document [ref=w1]:",
        "  - main [ref=w4]:",
        "    - heading \"Welcome\" [level=1] [ref=w40]",
        "    - heading \"Your job search\" [level=2] [ref=w41]",
        "    - link \"Saved jobs\" [ref=w50] [cursor=pointer]:",
        "      - /url: /dashboard/saved",
    ]
)


def make_settings(has_credentials: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        has_default_credentials=has_credentials,
        default_username="user@example.test" if has_credentials else "",
        default_password="secret" if has_credentials else "",
        gmail_credentials_path=Path("/missing/credentials.json"),
        gmail_token_path=Path("/missing/token.json"),
    )


class AuthenticateDefaultAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_node_runs_shared_agent_and_restores_job_page(self) -> None:
        tools = FakeTools(
            {
                "browser_navigate": [LOGIN_PAGE, JOB_PAGE],
                "browser_snapshot": [LOGIN_PAGE, JOB_PAGE],
            }
        )
        runtime = make_runtime(tools, FakeHumanChannel())
        captured: dict[str, Any] = {}

        async def fake_run_authentication_agent(**kwargs: Any) -> AuthenticationRun:
            captured.update(kwargs)
            return AuthenticationRun(
                summary="AUTHENTICATED - Simplify dashboard visible.",
                model_id="test/model",
                status="authenticated",
            )

        with (
            patch(
                "z_apply_core.nodes.authenticate_default_account.load_settings",
                return_value=make_settings(),
            ),
            patch(
                "z_apply_core.nodes.authenticate_default_account.run_authentication_agent",
                side_effect=fake_run_authentication_agent,
            ),
        ):
            result = await authenticate_default_account(
                {"runtime": runtime, "job_url": "https://jobs.example/job/1"},
                {"configurable": {"nim_router": NimRouter()}},
            )

        self.assertEqual(result["auth_status"], "authenticated")
        self.assertEqual(result["auth_model_id"], "test/model")
        self.assertEqual(result["auth_summary"], "AUTHENTICATED - Simplify dashboard visible.")
        self.assertEqual(
            tools.calls,
            [
                ("browser_navigate", {"url": SIMPLIFY_DASHBOARD_URL}),
                ("browser_snapshot", {}),
                ("browser_navigate", {"url": "https://jobs.example/job/1"}),
                ("browser_snapshot", {}),
            ],
        )
        # One shared browser tool set: the specialist set minus browser_tabs
        # and browser_take_screenshot (text-only agent; screenshots waste turns).
        self.assertEqual(tools.langchain_tool_requests, [AUTHENTICATION_BROWSER_TOOLS])
        self.assertEqual(
            set(AUTHENTICATION_BROWSER_TOOLS),
            set(AUTHENTICATION_SPECIALIST_BROWSER_TOOLS)
            - {"browser_tabs", "browser_take_screenshot"},
        )
        self.assertNotIn("browser_take_screenshot", AUTHENTICATION_BROWSER_TOOLS)
        tool_names = [getattr(tool, "name", "") for tool in captured["tools"]]
        self.assertIn("browser_auth_submit", tool_names)
        self.assertIn("browser_verify_link", tool_names)
        self.assertIn("request_manual_auth", tool_names)
        self.assertIn("DEFAULT_USERNAME and DEFAULT_PASSWORD are configured.", captured["task"])
        self.assertIsNotNone(captured["browser"])

    async def test_auth_verdict_survives_job_page_restore_failure(self) -> None:
        class RestoreFailingTools(FakeTools):
            def __init__(self) -> None:
                super().__init__(
                    {
                        "browser_navigate": [LOGIN_PAGE],
                        "browser_snapshot": [LOGIN_PAGE],
                    }
                )
                self.navigate_calls = 0

            async def call(self, name: str, arguments: dict[str, object] | None = None) -> str:
                if name == "browser_navigate":
                    self.navigate_calls += 1
                    if self.navigate_calls > 1:
                        raise RuntimeError("Page.goto: NS_ERROR_ABORT")
                return await super().call(name, arguments)

        tools = RestoreFailingTools()
        runtime = make_runtime(tools)

        async def fake_run_authentication_agent(**kwargs: Any) -> AuthenticationRun:
            return AuthenticationRun(
                summary="AUTHENTICATED - Simplify dashboard visible.",
                model_id="test/model",
                status="authenticated",
            )

        with (
            patch(
                "z_apply_core.nodes.authenticate_default_account.load_settings",
                return_value=make_settings(),
            ),
            patch(
                "z_apply_core.nodes.authenticate_default_account.run_authentication_agent",
                side_effect=fake_run_authentication_agent,
            ),
        ):
            result = await authenticate_default_account(
                {"runtime": runtime, "job_url": "https://jobs.example/job/1"},
                {"configurable": {"nim_router": NimRouter()}},
            )

        # The auth verdict must not be turned into a failure just because the
        # post-check restore navigation aborted; the orchestrator re-observes
        # the live page anyway.
        self.assertEqual(result["auth_status"], "authenticated")
        self.assertEqual(result["auth_summary"], "AUTHENTICATED - Simplify dashboard visible.")
        self.assertEqual(tools.navigate_calls, 2)

    async def test_auth_node_still_checks_persistent_session_without_credentials(self) -> None:
        tools = FakeTools(
            {
                "browser_navigate": [WELCOME_PAGE, JOB_PAGE],
                "browser_snapshot": [WELCOME_PAGE, JOB_PAGE],
            }
        )
        runtime = make_runtime(tools)
        captured: dict[str, Any] = {}

        async def fake_run_authentication_agent(**kwargs: Any) -> AuthenticationRun:
            captured.update(kwargs)
            return AuthenticationRun(
                summary="AUTHENTICATED - Saved Simplify session is authenticated.",
                model_id="test/model",
                status="authenticated",
            )

        with (
            patch(
                "z_apply_core.nodes.authenticate_default_account.load_settings",
                return_value=make_settings(has_credentials=False),
            ),
            patch(
                "z_apply_core.nodes.authenticate_default_account.run_authentication_agent",
                side_effect=fake_run_authentication_agent,
            ),
        ):
            result = await authenticate_default_account(
                {"runtime": runtime, "job_url": "https://jobs.example/job/1"},
                {"configurable": {"nim_router": NimRouter()}},
            )

        self.assertEqual(result["auth_status"], "authenticated")
        self.assertEqual(tools.calls[0][0], "browser_navigate")
        self.assertIn("No default credential secret keys are configured.", captured["task"])

    async def test_no_progress_stall_routes_to_not_verified_not_blocked(self) -> None:
        tools = FakeTools(
            {
                "browser_navigate": [LOGIN_PAGE, JOB_PAGE],
                "browser_snapshot": [LOGIN_PAGE, JOB_PAGE],
            }
        )
        runtime = make_runtime(tools, FakeHumanChannel())

        async def failing_run_authentication_agent(**kwargs: Any) -> AuthenticationRun:
            # DeepAgents re-raises middleware trips through untyped SDK errors;
            # a RuntimeError carrying the circuit message must still map to
            # not_verified so the orchestrator gets a chance mid-run.
            raise RuntimeError(
                "Tool activity (browser_find) did not advance the browser state; "
                "ending this agent turn so the persistent goal can recover from "
                "fresh evidence."
            )

        with (
            patch(
                "z_apply_core.nodes.authenticate_default_account.load_settings",
                return_value=make_settings(),
            ),
            patch(
                "z_apply_core.nodes.authenticate_default_account.run_authentication_agent",
                side_effect=failing_run_authentication_agent,
            ),
        ):
            result = await authenticate_default_account(
                {"runtime": runtime, "job_url": "https://jobs.example/job/1"},
                {"configurable": {"nim_router": NimRouter()}},
            )

        self.assertEqual(result["auth_status"], "not_verified")
        self.assertIn("stalled", result["auth_summary"])
        self.assertIn(
            ("browser_navigate", {"url": "https://jobs.example/job/1"}),
            tools.calls,
        )


if __name__ == "__main__":
    unittest.main()
