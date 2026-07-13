from __future__ import annotations

import asyncio
import base64
import email
import json
import re
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, ToolException, tool
from langchain_google_community import GmailToolkit
from langchain_google_community._utils import get_google_credentials
from langchain_google_community.gmail.utils import build_gmail_service

READONLY_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
ALLOWED_GMAIL_TOOLS = frozenset({"search_gmail"})
GMAIL_SEARCH_ATTEMPTS = 3
GMAIL_SEARCH_INTERVAL_SECONDS = 10


class ReadonlyGmailClient:
    """Lazily initialize the Gmail API and expose only read operations."""

    def __init__(self, *, credentials_path: Path, token_path: Path) -> None:
        self._credentials_path = credentials_path.expanduser().resolve()
        self._token_path = token_path.expanduser().resolve()
        self._tools: dict[str, BaseTool] | None = None
        self._resource: Any | None = None

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            tools = self._ensure_tools()
            if name == "get_gmail_message":
                result = self._read_message(arguments["message_id"])
            else:
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
        resource = build_gmail_service(credentials=credentials)
        self._resource = resource
        return {
            gmail_tool.name: gmail_tool
            for gmail_tool in GmailToolkit(api_resource=resource).get_tools()
            if gmail_tool.name in ALLOWED_GMAIL_TOOLS
        }

    def _read_message(self, message_id: str) -> dict[str, Any]:
        """Read a raw message without dropping HTML-only verification links."""
        if self._resource is None:
            raise ToolException("Gmail API resource is unavailable.")
        message_data = (
            self._resource.users()
            .messages()
            .get(userId="me", format="raw", id=message_id)
            .execute()
        )
        raw_message = base64.urlsafe_b64decode(message_data["raw"])
        parsed = email.message_from_bytes(raw_message, policy=policy.default)
        plain_parts: list[str] = []
        html_parts: list[str] = []

        for part in parsed.walk() if parsed.is_multipart() else (parsed,):
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            content = part.get_content()
            if not isinstance(content, str):
                continue
            if content_type == "text/plain":
                plain_parts.append(content)
            else:
                html_parts.append(content)

        html_reader = _ReadableHtml()
        for html_part in html_parts:
            html_reader.feed(html_part)
        body = _normalize_body("\n".join(plain_parts))
        if not body:
            body = html_reader.text

        return {
            "id": message_id,
            "threadId": message_data.get("threadId", ""),
            "snippet": message_data.get("snippet", ""),
            "body": body,
            "links": html_reader.links,
            "subject": str(parsed.get("Subject", "")),
            "sender": str(parsed.get("From", "")),
            "date": str(parsed.get("Date", "")),
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
        """Read one Gmail message, including full HTML-only body text and links."""
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


class _ReadableHtml(HTMLParser):
    """Extract readable text and explicit links from an HTML email part."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._text: list[str] = []
        self.links: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        if value.startswith(("https://", "http://")):
            value = re.sub(r"\s+", "", value)
            self._record_link(value)
        self._text.append(value)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._record_link(href)

    def _record_link(self, value: str) -> None:
        if value.startswith(("https://", "http://")) and value not in self.links:
            self.links.append(value)

    @property
    def text(self) -> str:
        return _normalize_body("\n".join(self._text))


def _normalize_body(value: str) -> str:
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())
