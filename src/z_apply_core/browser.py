from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright_python_mcp.mcp import create_connection


async def open_job_with_browser_tools(job_url: str) -> str:
    workspace_dir = Path.cwd() / ".z-apply"
    profile_dir = workspace_dir / "browser-profile"
    output_dir = workspace_dir / "browser-artifacts"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    server = await create_connection(
        {
            "browser": {
                "provider": "camoufox",
                "browserName": "firefox",
                "userDataDir": str(profile_dir),
                "camoufoxOptions": {"no_viewport": True},
            },
            "outputDir": str(output_dir),
            "outputMode": "stdout",
        }
    )
    backend = server.backend
    try:
        navigate_result = await backend.call_tool("browser_navigate", {"url": job_url})
        if _is_error(navigate_result):
            return _text_content(navigate_result)
        snapshot_result = await backend.call_tool("browser_snapshot", {}, meta={"raw": True})
        return _text_content(snapshot_result)
    finally:
        await backend.close()


def _is_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", False))


def _text_content(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [getattr(item, "text", None) for item in content]
        return "\n".join(part for part in parts if isinstance(part, str))
    return str(content)
