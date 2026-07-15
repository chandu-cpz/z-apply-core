from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.tools import tool

from z_apply_core.agents.capability_context import CapabilityContextMiddleware
from z_apply_core.browser_observation import BrowserCapabilities
from z_apply_core.browser_session import BrowserSession


@tool
def browser_observe() -> str:
    """Observe."""
    return "observed"


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
            BrowserCapabilities(required_file_upload_pending=True),
        )

        self.assertEqual(
            [tool.name for tool in tools],
            ["browser_observe", "browser_fill_form", "browser_click_upload"],
        )

    def test_ordinary_form_excludes_deepagents_filesystem_tools(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(editable_controls_visible=True),
        )

        self.assertEqual(tools, self.tools[:-1])


class BrowserCapabilityParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_session_returns_compositional_capabilities(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "editable_controls_visible": True,
                    "auth_gate_visible": True,
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
        self.assertFalse(capabilities.required_file_upload_pending)


if __name__ == "__main__":
    unittest.main()
