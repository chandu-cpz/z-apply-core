from __future__ import annotations

import unittest
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from z_apply_core.agents.capability_context import (
    CAPABILITY_CONTEXT_SOURCE,
    CapabilityContextMiddleware,
)
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
def application_submitted() -> str:
    """Finish."""
    return "finished"


@tool
def ls() -> str:
    """List files."""
    return "files"


@tool
def lookup_candidate_memory() -> str:
    """Look up candidate memory."""
    return "found"


class CapabilityContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = [
            browser_observe,
            browser_click,
            browser_navigate,
            browser_fill_form,
            browser_click_upload,
            task,
            application_submitted,
            ls,
            lookup_candidate_memory,
        ]

    def test_browser_state_does_not_hide_safe_agent_actions(self) -> None:
        expected = [
            "browser_observe",
            "browser_click",
            "browser_navigate",
            "browser_fill_form",
            "browser_click_upload",
            "task",
            "application_submitted",
            "lookup_candidate_memory",
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

    def test_candidate_memory_lookup_survives_capability_filtering(self) -> None:
        tools = CapabilityContextMiddleware._filter_tools(
            self.tools,
            BrowserCapabilities(editable_controls_visible=True),
        )

        self.assertIn("lookup_candidate_memory", [tool.name for tool in tools])

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


class UploadContextGatingTests(unittest.TestCase):
    """The runtime must push an upload only when one is genuinely required.

    An optional empty upload control must never be presented as required
    work: once the resume is attached, the agent should ignore remaining
    empty upload controls instead of chasing them.
    """

    def _context_text(self, capabilities: BrowserCapabilities | None) -> str:
        import asyncio

        from langchain.agents.middleware import ModelRequest
        from langchain.agents.middleware.types import ModelResponse
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        class StubBrowser:
            pending_atomic_upload_target = ""
            current_observation = None

            async def inspect_capabilities(self) -> BrowserCapabilities | None:
                return capabilities

        middleware = CapabilityContextMiddleware(StubBrowser())  # type: ignore[arg-type]
        captured: dict[str, Any] = {}

        async def handler(request: Any) -> ModelResponse[Any]:
            captured["request"] = request
            return ModelResponse(result=[])

        async def run() -> None:
            request = ModelRequest(
                model=GenericFakeChatModel(messages=iter(["ok"])),
                messages=[HumanMessage(content="go")],
                tools=[browser_click_upload],
            )
            await middleware.awrap_model_call(request, handler)

        asyncio.run(run())
        messages = captured["request"].messages
        context = next(
            message
            for message in messages
            if getattr(message, "name", None) == CAPABILITY_CONTEXT_SOURCE
        )
        return str(context.content)

    def test_required_upload_pending_pushes_upload(self) -> None:
        text = self._context_text(
            BrowserCapabilities(
                empty_file_upload_present=True,
                required_file_upload_pending=True,
            )
        )

        self.assertIn("required_file_upload_pending=true", text)
        self.assertIn("REQUIRED file upload is still empty", text)

    def test_optional_empty_upload_is_presented_as_not_work(self) -> None:
        text = self._context_text(
            BrowserCapabilities(
                empty_file_upload_present=True,
                required_file_upload_pending=False,
            )
        )

        self.assertIn("optional_empty_upload_present=true", text)
        self.assertIn("It is not work", text)
        self.assertNotIn("must be attached", text)

    def test_no_upload_flags_emit_no_upload_context(self) -> None:
        text = self._context_text(BrowserCapabilities())

        self.assertNotIn("file upload", text)
