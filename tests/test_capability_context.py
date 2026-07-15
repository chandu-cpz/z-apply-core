from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.tools import tool

from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.browser_observation import BrowserCapabilities, BrowserObservation
from z_apply_core.browser_session import BrowserSession


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
            request_submit_approval,
            application_submitted,
            ls,
        ]

    def test_auth_gate_keeps_read_and_delegation_but_hides_mutations(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(auth_gate_visible=True),
        )

        self.assertEqual([tool.name for tool in tools], ["browser_observe", "task"])

    def test_required_upload_hides_delegation_and_submission(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(
                editable_controls_visible=True,
                required_file_upload_pending=True,
            ),
        )

        self.assertEqual(
            [tool.name for tool in tools],
            [
                "browser_observe",
                "browser_click",
                "browser_navigate",
                "browser_fill_form",
                "browser_click_upload",
            ],
        )

    def test_intercepted_file_chooser_exposes_only_atomic_upload(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(editable_controls_visible=True),
            atomic_upload_pending=True,
        )

        self.assertEqual([tool.name for tool in tools], ["browser_click_upload"])

    def test_hidden_empty_file_input_hides_generic_click(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(empty_file_upload_present=True),
        )

        self.assertEqual(
            [tool.name for tool in tools],
            ["browser_observe", "browser_click_upload"],
        )

    def test_ordinary_form_excludes_deepagents_filesystem_tools(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(editable_controls_visible=True),
        )

        self.assertEqual(
            [tool.name for tool in tools],
            [
                "browser_observe",
                "browser_click",
                "browser_navigate",
                "browser_fill_form",
                "browser_click_upload",
                "request_submit_approval",
                "application_submitted",
            ],
        )

    def test_unresolved_required_control_exposes_answer_writer_delegation(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(
                editable_controls_visible=True,
                unresolved_required_controls=1,
            ),
        )

        self.assertIn("task", [tool.name for tool in tools])

    def test_job_detail_page_hides_form_mutations_and_human_delegation(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(editable_controls_visible=False),
        )

        self.assertEqual(
            [tool.name for tool in tools],
            [
                "browser_observe",
                "browser_click",
                "browser_navigate",
                "application_submitted",
            ],
        )

    def test_capability_inspection_failure_exposes_only_safe_recovery_tools(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(self.tools, None)

        self.assertEqual([tool.name for tool in tools], ["browser_observe"])

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
        self.assertIn("bounded current-page view", rendered)


class BrowserCapabilityParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_session_returns_compositional_capabilities(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "editable_controls_visible": True,
                    "auth_gate_visible": True,
                    "empty_file_upload_present": True,
                    "required_file_upload_pending": False,
                    "enabled_form_submit_visible": True,
                }
            )
        )
        backend = SimpleNamespace(_ensure_tab=AsyncMock(return_value=SimpleNamespace(page=page)))
        browser = object.__new__(BrowserSession)
        browser._backend = backend
        browser._mutation_gate = None
        browser._lease = None

        capabilities = await browser.inspect_capabilities()

        self.assertTrue(capabilities.auth_gate_visible)
        self.assertTrue(capabilities.editable_controls_visible)
        self.assertTrue(capabilities.empty_file_upload_present)
        self.assertFalse(capabilities.required_file_upload_pending)


if __name__ == "__main__":
    unittest.main()
