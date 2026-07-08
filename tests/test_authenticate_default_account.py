from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from z_apply_core.nodes.authenticate_default_account import (
    SIMPLIFY_DASHBOARD_URL,
    authenticate_default_account,
    classify_auth_snapshot,
)
from z_apply_core.runtime import RunRuntime


class FakeTools:
    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, name: str, arguments: dict[str, object] | None = None) -> str:
        self.calls.append((name, arguments or {}))
        values = self.responses.get(name)
        if values:
            return values.pop(0)
        return ""


class FakeHumanChannel:
    def __init__(self) -> None:
        self.questions: list[dict[str, object]] = []

    async def ask(self, **kwargs: object) -> str:
        self.questions.append(kwargs)
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


class AuthClassificationTests(unittest.TestCase):
    def test_login_form_is_detected(self) -> None:
        result = classify_auth_snapshot(
            '- textbox "Email" [ref=e1]\n- textbox "Password" [ref=e2]\n- button "Login"'
        )

        self.assertEqual(result.status, "login_required")

    def test_authenticated_state_is_detected(self) -> None:
        result = classify_auth_snapshot(
            "- Page URL: https://simplify.jobs/dashboard\n- text: Applications"
        )

        self.assertEqual(result.status, "authenticated")

    def test_blocker_state_is_detected(self) -> None:
        result = classify_auth_snapshot("- text: Enter verification code sent to your email")

        self.assertEqual(result.status, "blocked")


class AuthenticateDefaultAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_credentials_are_filled_and_job_page_is_restored(self) -> None:
        login_snapshot = (
            '- textbox "Email" [ref=e11]\n'
            '- textbox "Password" [ref=e12]\n'
            '- button "Login" [ref=e13]'
        )
        tools = FakeTools(
            {
                "browser_navigate": [
                    login_snapshot,
                    "- heading Job details",
                ],
                "browser_type": [
                    "- textbox email filled",
                    "- submitted",
                ],
                "browser_wait_for": ["- waited"],
                "browser_snapshot": [
                    "- Page URL: https://simplify.jobs/dashboard\n- text: Applications",
                    "- heading Job details",
                ],
            }
        )
        runtime = make_runtime(tools)
        settings = SimpleNamespace(
            has_default_credentials=True,
            default_username="user@example.test",
            default_password="secret",
        )

        with patch(
            "z_apply_core.nodes.authenticate_default_account.load_settings",
            return_value=settings,
        ):
            result = await authenticate_default_account(
                {"runtime": runtime, "job_url": "https://jobs.example/job/1"},
                {},
            )

        self.assertEqual(result["auth_status"], "authenticated")
        self.assertEqual(
            tools.calls,
            [
                ("browser_navigate", {"url": SIMPLIFY_DASHBOARD_URL}),
                (
                    "browser_type",
                    {
                        "target": "e11",
                        "element": "default account email field",
                        "text": "user@example.test",
                    },
                ),
                (
                    "browser_type",
                    {
                        "target": "e12",
                        "element": "default account password field",
                        "text": "secret",
                        "submit": True,
                    },
                ),
                ("browser_wait_for", {"time": 2}),
                ("browser_snapshot", {}),
                ("browser_navigate", {"url": "https://jobs.example/job/1"}),
                ("browser_snapshot", {}),
            ],
        )

    async def test_blocker_uses_human_channel_then_rechecks(self) -> None:
        human = FakeHumanChannel()
        tools = FakeTools(
            {
                "browser_navigate": [
                    "- text: Enter OTP",
                    "- heading Job details",
                ],
                "browser_snapshot": [
                    "- Page URL: https://simplify.jobs/dashboard\n- text: Applications",
                    "- heading Job details",
                ],
            }
        )
        runtime = make_runtime(tools, human)
        settings = SimpleNamespace(
            has_default_credentials=True,
            default_username="user@example.test",
            default_password="secret",
        )

        with patch(
            "z_apply_core.nodes.authenticate_default_account.load_settings",
            return_value=settings,
        ):
            result = await authenticate_default_account(
                {"runtime": runtime, "job_url": "https://jobs.example/job/1"},
                {},
            )

        self.assertEqual(result["auth_status"], "authenticated")
        self.assertEqual(human.questions[0]["options"], ["Done"])


if __name__ == "__main__":
    unittest.main()
