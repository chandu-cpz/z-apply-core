from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from langchain_core.tools import BaseTool
from mcp.types import ImageContent, TextContent

from z_apply_core.agents.specialists.vision import build_vision_specialist
from z_apply_core.browser_session import (
    BrowserSession,
    BrowserToolExecutionError,
    _content_blocks,
)
from z_apply_core.browser_tools import BrowserToolRegistry


class MultimodalBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_filenames_are_confined_to_artifact_workspace(self) -> None:
        backend = SimpleNamespace(
            call_tool=AsyncMock(return_value=SimpleNamespace(content=[])),
            close=AsyncMock(),
        )
        server = SimpleNamespace(
            backend=backend,
            backend_pool=SimpleNamespace(tools=[]),
        )
        session = BrowserSession(server)

        await session.call_tool(
            "browser_snapshot",
            {"filename": "auth_alert_snapshot", "target": "e1"},
        )
        await session.call_tool_content(
            "browser_take_screenshot",
            {"filename": "root-leak.png", "fullPage": True},
        )
        await session.call_tool(
            "browser_file_upload",
            {"paths": [".z-apply/input/resume.pdf"]},
        )

        self.assertEqual(
            backend.call_tool.await_args_list[0].args,
            (
                "browser_snapshot",
                {"filename": "auth_alert_snapshot", "target": "e1"},
            ),
        )
        self.assertEqual(
            backend.call_tool.await_args_list[1].args,
            (
                "browser_take_screenshot",
                {"filename": "root-leak.png", "fullPage": True},
            ),
        )
        expected_workspace = str(Path.cwd() / ".z-apply" / "browser-artifacts")
        self.assertEqual(
            backend.call_tool.await_args_list[0].kwargs["meta"],
            {"raw": True, "cwd": expected_workspace},
        )
        self.assertEqual(
            backend.call_tool.await_args_list[1].kwargs["meta"],
            {"raw": True, "cwd": expected_workspace},
        )
        self.assertEqual(
            backend.call_tool.await_args_list[2].kwargs["meta"],
            {"raw": True},
        )

    async def test_backend_error_result_raises_typed_tool_error(self) -> None:
        backend = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(
                    content="Error: browser cannot inspect the current state",
                    is_error=True,
                )
            ),
            close=AsyncMock(),
        )
        server = SimpleNamespace(
            backend=backend,
            backend_pool=SimpleNamespace(tools=[]),
        )
        session = BrowserSession(server)

        with self.assertRaises(BrowserToolExecutionError):
            await session.call_tool("browser_snapshot", {})

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
