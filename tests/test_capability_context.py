from __future__ import annotations

import unittest

from langchain_core.tools import tool

from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.browser_observation import BrowserCapabilities, BrowserObservation


@tool
def browser_observe() -> str:
    """Observe."""
    return "observed"


@tool
def browser_click() -> str:
    """Click."""
    return "clicked"


@tool
def browser_navigate() -> str:
    """Navigate."""
    return "navigated"


@tool
def browser_fill_form() -> str:
    """Fill."""
    return "filled"


@tool
def browser_click_upload() -> str:
    """Upload."""
    return "uploaded"


@tool
def task() -> str:
    """Delegate."""
    return "delegated"


@tool
def resolve_candidate_field() -> str:
    """Resolve one candidate field."""
    return "resolved"


@tool
def request_submit_approval() -> str:
    """Request approval."""
    return "requested"


@tool
def application_submitted() -> str:
    """Finish."""
    return "finished"


@tool
def ls() -> str:
    """List files."""
    return "files"


class CapabilityContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = [
            browser_observe,
            browser_click,
            browser_navigate,
            browser_fill_form,
            browser_click_upload,
            task,
            resolve_candidate_field,
            request_submit_approval,
            application_submitted,
            ls,
        ]

    def test_browser_state_does_not_hide_safe_agent_actions(self) -> None:
        expected = [
            "browser_observe",
            "browser_click",
            "browser_navigate",
            "browser_fill_form",
            "browser_click_upload",
            "task",
            "resolve_candidate_field",
            "request_submit_approval",
            "application_submitted",
        ]
        states = (
            BrowserCapabilities(auth_gate_visible=True),
            BrowserCapabilities(editable_controls_visible=True),
            BrowserCapabilities(editable_controls_visible=False),
            None,
        )

        for state in states:
            with self.subTest(state=state):
                tools = CapabilityContextMiddleware._filter_tools(self.tools, state)
                self.assertEqual([tool.name for tool in tools], expected)

    def test_intercepted_file_chooser_exposes_only_atomic_upload(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(editable_controls_visible=True),
            atomic_upload_pending=True,
        )

        self.assertEqual([tool.name for tool in tools], ["browser_click_upload"])

    def test_compact_observation_bounds_repeated_model_context(self) -> None:
        evidence = "\n".join(
            [f"- generic filler {index} {'x' * 80}" for index in range(300)]
            + ['- textbox "Email" [ref=e500]', '- button "Continue" [ref=e501]']
        )
        observation = BrowserObservation.create(
            revision=7,
            url="https://example.test/apply",
            title="Apply",
            evidence=evidence,
        )

        rendered = observation.compact_render(max_chars=2_000)

        self.assertLessEqual(len(rendered), 2_000)
        self.assertIn("https://example.test/apply", rendered)
        self.assertIn('textbox "Email" [ref=e500]', rendered)
        self.assertIn('button "Continue" [ref=e501]', rendered)
        self.assertIn("bounded current-page view", rendered)

    def test_compact_observation_keeps_field_question_with_generic_textbox_name(
        self,
    ) -> None:
        evidence = "\n".join(
            [f"- generic filler {index} {'x' * 80}" for index in range(80)]
            + [
                "- listitem [ref=e90]:",
                "  - generic [ref=e91]:",
                "    - generic [ref=e93]:",
                "      - text: Where did you hear about Resilinc?",
                '      - textbox "Type your response" [ref=e96]',
            ]
        )
        observation = BrowserObservation.create(
            revision=8,
            url="https://example.test/apply",
            title="Apply",
            evidence=evidence,
        )

        rendered = observation.compact_render(max_chars=2_000)

        self.assertIn("Where did you hear about Resilinc?", rendered)
        self.assertIn('textbox "Type your response" [ref=e96]', rendered)


if __name__ == "__main__":
    unittest.main()
