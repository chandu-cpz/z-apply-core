from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.tools import BaseTool
from mcp.types import ImageContent, TextContent

from z_apply_core.agents.specialists.vision import build_vision_specialist
from z_apply_core.browser_session import _content_blocks
from z_apply_core.browser_tools import BrowserToolRegistry


class MultimodalBrowserTests(unittest.IsolatedAsyncioTestCase):
    def test_mcp_image_content_becomes_standard_langchain_block(self) -> None:
        result = SimpleNamespace(
            content=[
                TextContent(type="text", text="Screenshot of current viewport"),
                ImageContent(type="image", data="cG5n", mimeType="image/png"),
            ]
        )

        self.assertEqual(
            _content_blocks(result),
            [
                {"type": "text", "text": "Screenshot of current viewport"},
                {
                    "type": "image",
                    "base64": "cG5n",
                    "mime_type": "image/png",
                },
            ],
        )

    async def test_screenshot_langchain_tool_uses_multimodal_caller(self) -> None:
        calls: list[str] = []

        async def text_caller(name: str, _arguments: dict[str, Any]) -> str:
            calls.append(f"text:{name}")
            return "text only"

        async def multimodal_caller(
            name: str,
            _arguments: dict[str, Any],
        ) -> list[dict[str, str]]:
            calls.append(f"multimodal:{name}")
            return [{"type": "image", "base64": "cG5n", "mime_type": "image/png"}]

        spec = SimpleNamespace(
            name="browser_take_screenshot",
            title="Take screenshot",
            description="Capture the page",
            parameters=(),
        )
        registry = BrowserToolRegistry(
            [spec],
            text_caller,
            langchain_callers={"browser_take_screenshot": multimodal_caller},
        )

        result = await registry.langchain_tools().pop().ainvoke({})

        self.assertEqual(calls, ["multimodal:browser_take_screenshot"])
        self.assertEqual(result[0]["type"], "image")

    def test_vision_specialist_receives_only_screenshot_tool(self) -> None:
        screenshot = cast(BaseTool, SimpleNamespace(name="browser_take_screenshot"))
        snapshot = cast(BaseTool, SimpleNamespace(name="browser_snapshot"))

        specialist = build_vision_specialist([snapshot, screenshot])

        self.assertEqual(specialist["tools"], [screenshot])


if __name__ == "__main__":
    unittest.main()
