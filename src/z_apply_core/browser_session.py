from __future__ import annotations

from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from langchain_core.tools import ToolException
from playwright_python_mcp.mcp import create_connection

from z_apply_core.browser_config import build_browser_config
from z_apply_core.browser_tools import (
    BROWSER_CHANGING_TOOL_NAMES,
    BrowserToolRegistry,
    normalize_browser_arguments,
)

INLINE_CAPTURE_TOOLS = frozenset(
    {"browser_snapshot", "browser_take_screenshot", "browser_pdf"}
)


class BrowserToolExecutionError(ToolException):
    """A browser backend tool result explicitly marked as an execution error."""


class BrowserSession:
    def __init__(self, server: Any, *, run_id: str) -> None:
        self._server = server
        self._backend = server.backend
        self.run_id = run_id
        self._capture_workspace = Path.cwd() / ".z-apply" / "runs" / run_id / "browser-artifacts"
        self.tools = BrowserToolRegistry(
            tuple(server.backend_pool.tools),
            self.call_tool,
            langchain_callers={
                **{
                    name: self.call_tool_with_inline_snapshot
                    for name in BROWSER_CHANGING_TOOL_NAMES
                    if name != "browser_click_upload"
                },
                "browser_take_screenshot": self.call_tool_content,
            },
        )

    @classmethod
    async def start(cls, *, run_id: str | None = None) -> Self:
        resolved_run_id = run_id or uuid4().hex
        return cls(
            await create_connection(build_browser_config(resolved_run_id)), run_id=resolved_run_id
        )

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        normalized = normalize_browser_arguments(arguments)
        if name == "browser_snapshot" and "target" not in normalized:
            normalized["target"] = "html"
        result = await self._backend.call_tool(
            name,
            normalized,
            meta=self._call_meta(name),
        )
        _raise_for_tool_error(name, result)
        return _text_content(result)

    async def call_tool_content(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        """Return MCP text and image results as LangChain standard content blocks."""
        result = await self._backend.call_tool(
            name,
            normalize_browser_arguments(arguments),
            meta=self._call_meta(name),
        )
        _raise_for_tool_error(name, result)
        return _content_blocks(result)

    async def call_tool_with_inline_snapshot(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute a mutation and return current inline evidence when available."""
        mutation = await self.call_tool(name, arguments)
        try:
            return await self.call_tool("browser_snapshot")
        except BrowserToolExecutionError as exc:
            return f"{mutation}\nPost-action inline snapshot unavailable: {exc}"

    async def upload_files(self, target: str, paths: list[str]) -> str:
        """Resolve a current ARIA target and attach files without native chooser state."""
        tab = await self._backend._ensure_tab()
        resolved = await tab.resolve_target(target=target)
        locator = resolved.locator
        is_file_input = await locator.evaluate(
            "element => element instanceof HTMLInputElement && element.type === 'file'"
        )
        if not is_file_input:
            file_inputs = locator.locator("input[type=file]")
            count = await file_inputs.count()
            if count != 1:
                raise BrowserToolExecutionError(
                    f"Upload target {target!r} is not a file input and contains "
                    f"{count} file inputs."
                )
            locator = file_inputs
        await locator.set_input_files(paths)
        evidence = await self.call_tool("browser_snapshot")
        return "Files attached directly to the resolved upload control.\n" + evidence

    async def close(self) -> None:
        await self._backend.close()

    def artifact_path(self, filename: str) -> Path:
        """Return the run-owned path used by browser capture tools."""
        return (self._capture_workspace / filename).resolve()

    def _call_meta(self, name: str) -> dict[str, object]:
        meta: dict[str, object] = {"raw": True}
        if name in INLINE_CAPTURE_TOOLS:
            meta["cwd"] = str(self._capture_workspace)
        return meta


def _text_content(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [getattr(item, "text", None) for item in content]
        return "\n".join(part for part in parts if isinstance(part, str))
    return str(content)


def _raise_for_tool_error(name: str, result: Any) -> None:
    if bool(getattr(result, "is_error", False) or getattr(result, "isError", False)):
        raise BrowserToolExecutionError(f"{name} failed: {_text_content(result)}")


def _content_blocks(result: Any) -> list[dict[str, str]]:
    content = getattr(result, "content", result)
    if not isinstance(content, list):
        return [{"type": "text", "text": _text_content(result)}]

    blocks: list[dict[str, str]] = []
    for item in content:
        item_type = getattr(item, "type", None)
        if item_type == "text":
            text = getattr(item, "text", None)
            if isinstance(text, str):
                blocks.append({"type": "text", "text": text})
        elif item_type == "image":
            data = getattr(item, "data", None)
            mime_type = getattr(item, "mimeType", None)
            if isinstance(data, str) and isinstance(mime_type, str):
                blocks.append(
                    {
                        "type": "image",
                        "base64": data,
                        "mime_type": mime_type,
                    }
                )
    return blocks or [{"type": "text", "text": _text_content(result)}]
