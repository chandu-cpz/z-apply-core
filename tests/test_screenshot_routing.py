from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp.types import ImageContent, TextContent

from z_apply_core.browser_config import build_browser_config
from z_apply_core.browser_session import VISUAL_EVIDENCE_UNAVAILABLE_NOTE, BrowserSession


def _session_with_backend(content: object, **flags: bool) -> tuple[BrowserSession, AsyncMock]:
    backend = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(content=content, **flags)),
        close=AsyncMock(),
    )
    server = SimpleNamespace(
        backend=backend,
        backend_pool=SimpleNamespace(tools=[]),
    )
    return BrowserSession(server, run_id="test-run"), backend.call_tool


def _image_result() -> list[object]:
    return [
        TextContent(type="text", text="Screenshot captured"),
        ImageContent(type="image", data="cG5n", mimeType="image/png"),
    ]


class ScreenshotFilenameInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_screenshot_without_filename_gets_incrementing_deterministic_names(self) -> None:
        session, call_tool = _session_with_backend(_image_result())

        await session.call_tool_content("browser_take_screenshot", {})
        await session.call_tool_content("browser_take_screenshot", {})

        self.assertEqual(
            call_tool.await_args_list[0].args[1]["filename"],
            "screenshot_000.png",
        )
        self.assertEqual(
            call_tool.await_args_list[1].args[1]["filename"],
            "screenshot_001.png",
        )

    async def test_explicit_screenshot_filename_is_preserved_and_does_not_consume_counter(
        self,
    ) -> None:
        session, call_tool = _session_with_backend(_image_result())

        await session.call_tool_content(
            "browser_take_screenshot",
            {"filename": "root-leak.png", "fullPage": True},
        )
        await session.call_tool_content("browser_take_screenshot", {})

        self.assertEqual(call_tool.await_args_list[0].args[1]["filename"], "root-leak.png")
        self.assertEqual(call_tool.await_args_list[0].args[1]["fullPage"], True)
        self.assertEqual(
            call_tool.await_args_list[1].args[1]["filename"],
            "screenshot_000.png",
        )

    async def test_counter_continues_after_explicit_filename_call(self) -> None:
        session, call_tool = _session_with_backend(_image_result())

        await session.call_tool_content("browser_take_screenshot", {})
        await session.call_tool_content(
            "browser_take_screenshot",
            {"filename": "explicit.png"},
        )
        await session.call_tool_content("browser_take_screenshot", {})

        self.assertEqual(call_tool.await_args_list[0].args[1]["filename"], "screenshot_000.png")
        self.assertEqual(call_tool.await_args_list[1].args[1]["filename"], "explicit.png")
        self.assertEqual(call_tool.await_args_list[2].args[1]["filename"], "screenshot_001.png")


class ScreenshotVisualEvidenceGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_screenshot_backend_error_yields_typed_note_without_raising(self) -> None:
        session, _call_tool = _session_with_backend(content="backend failure", is_error=True)

        blocks = await session.call_tool_content("browser_take_screenshot", {})

        self.assertEqual(
            blocks,
            [{"type": "text", "text": VISUAL_EVIDENCE_UNAVAILABLE_NOTE}],
        )

    async def test_screenshot_without_retained_image_yields_typed_note(self) -> None:
        session, _call_tool = _session_with_backend(
            [TextContent(type="text", text="saved to /runs/x/browser-artifacts")],
        )

        blocks = await session.call_tool_content("browser_take_screenshot", {})

        self.assertEqual(
            blocks,
            [{"type": "text", "text": VISUAL_EVIDENCE_UNAVAILABLE_NOTE}],
        )

    async def test_screenshot_with_retained_image_returns_image_block(self) -> None:
        session, _call_tool = _session_with_backend(_image_result())

        blocks = await session.call_tool_content("browser_take_screenshot", {})

        self.assertEqual(
            blocks,
            [
                {"type": "text", "text": "Screenshot captured"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}},
            ],
        )


class BrowserConfigObservabilityDefaultsTests(unittest.TestCase):
    def _build_config(self) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                default_username="",
                default_password="",
                camoufox_browser="official/150.0.2-alpha.26",
                simplify_addon_path=Path(directory),
            )
            with patch("z_apply_core.browser_config.load_settings", return_value=settings):
                return build_browser_config()

    def test_bounded_observability_defaults_are_present(self) -> None:
        config = self._build_config()

        self.assertEqual(config["outputMode"], "stdout")
        self.assertEqual(config["imageResponses"], "omit")
        self.assertEqual(config["console"], {"level": "error"})
        self.assertEqual(config["snapshot"], {"mode": "full"})


if __name__ == "__main__":
    unittest.main()
