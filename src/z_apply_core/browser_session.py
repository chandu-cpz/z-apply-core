from __future__ import annotations

from typing import Any, Self

from playwright_python_mcp.mcp import create_connection

from z_apply_core.browser_config import build_browser_config
from z_apply_core.browser_tools import BrowserToolRegistry


class BrowserSession:
    def __init__(self, server: Any) -> None:
        self._server = server
        self._backend = server.backend
        self.tools = BrowserToolRegistry(tuple(server.backend_pool.tools), self.call_tool)

    @classmethod
    async def start(cls) -> Self:
        return cls(await create_connection(build_browser_config()))

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        result = await self._backend.call_tool(name, arguments or {}, meta={"raw": True})
        return _text_content(result)

    async def close(self) -> None:
        await self._backend.close()


def _text_content(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [getattr(item, "text", None) for item in content]
        return "\n".join(part for part in parts if isinstance(part, str))
    return str(content)
