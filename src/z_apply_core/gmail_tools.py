from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, ToolException, tool
from langchain_google_community import GmailToolkit
from langchain_google_community._utils import get_google_credentials
from langchain_google_community.gmail.utils import build_resource_service

READONLY_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
ALLOWED_GMAIL_TOOLS = frozenset(
    {"search_gmail", "get_gmail_message"}
)
GMAIL_SEARCH_ATTEMPTS = 3
GMAIL_SEARCH_INTERVAL_SECONDS = 10


class ReadonlyGmailClient:
    """Lazily initialize the Gmail API and expose only read operations."""

    def __init__(self, *, credentials_path: Path, token_path: Path) -> None:
        self._credentials_path = credentials_path.expanduser().resolve()
        self._token_path = token_path.expanduser().resolve()
        self._tools: dict[str, BaseTool] | None = None

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            tools = self._ensure_tools()
            selected = tools.get(name)
            if selected is None:
                raise ToolException(f"Read-only Gmail operation {name!r} is unavailable.")
            result = selected.invoke(arguments)
            if name == "search_gmail":
                for _ in range(GMAIL_SEARCH_ATTEMPTS - 1):
                    if not _empty_search_result(result):
                        break
                    await asyncio.sleep(GMAIL_SEARCH_INTERVAL_SECONDS)
                    result = selected.invoke(arguments)
            return _safe_tool_content(result)
        except Exception as exc:  # noqa: BLE001 - surface a safe recoverable tool failure
            raise ToolException(f"Gmail read failed: {exc}") from exc

    def _ensure_tools(self) -> dict[str, BaseTool]:
        if self._tools is None:
            self._tools = self._build_tools()
        return self._tools

    def _build_tools(self) -> dict[str, BaseTool]:
        if not self._credentials_path.is_file() or not self._token_path.is_file():
            raise ToolException(
                "Gmail OAuth files are not configured; use the human verification fallback."
            )
        credentials = get_google_credentials(
            scopes=[READONLY_GMAIL_SCOPE],
            token_file=str(self._token_path),
            client_secrets_file=str(self._credentials_path),
        )
        resource = build_resource_service(credentials=credentials)
        return {
            gmail_tool.name: gmail_tool
            for gmail_tool in GmailToolkit(api_resource=resource).get_tools()
            if gmail_tool.name in ALLOWED_GMAIL_TOOLS
        }


def make_gmail_tools(*, credentials_path: Path, token_path: Path) -> list[BaseTool]:
    """Return lazy, read-only Gmail tools suitable for an auth specialist."""
    client = ReadonlyGmailClient(
        credentials_path=credentials_path,
        token_path=token_path,
    )

    @tool
    async def search_gmail(query: str, max_results: int = 10) -> Any:
        """Search recent Gmail messages with Gmail query syntax; never mutates mail."""
        return await client.invoke(
            "search_gmail",
            {"query": query, "resource": "messages", "max_results": max_results},
        )

    @tool
    async def get_gmail_message(message_id: str) -> Any:
        """Read one Gmail message previously identified by search_gmail."""
        return await client.invoke("get_gmail_message", {"message_id": message_id})

    for gmail_tool in (search_gmail, get_gmail_message):
        gmail_tool.handle_tool_error = True
    return [search_gmail, get_gmail_message]


def _safe_tool_content(value: Any) -> str:
    """Keep Gmail results valid at the model provider's tool-message boundary."""
    if isinstance(value, str):
        return value
    if value is None:
        return "GMAIL_TOOL_RETURNED_NO_RESULT"
    return json.dumps(value, ensure_ascii=False, default=str)


def _empty_search_result(value: Any) -> bool:
    return value is None or value == [] or value == {} or value == ""
