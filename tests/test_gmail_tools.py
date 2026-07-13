from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.tools import tool

from z_apply_core.gmail_tools import READONLY_GMAIL_SCOPE, make_gmail_tools


class GmailToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_gmail_initializes_lazily_with_readonly_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials_path = root / "credentials.json"
            token_path = root / "token.json"
            credentials_path.write_text("{}", encoding="utf-8")
            token_path.write_text("{}", encoding="utf-8")

            @tool
            def search_gmail(query: str, resource: str, max_results: int) -> dict[str, object]:
                """Fake Gmail search."""
                return {
                    "query": query,
                    "resource": resource,
                    "max_results": max_results,
                }

            @tool
            def get_gmail_message(message_id: str) -> dict[str, str]:
                """Fake Gmail message read."""
                return {"id": message_id}

            @tool
            def get_gmail_thread(thread_id: str) -> dict[str, str]:
                """Fake Gmail thread read."""
                return {"id": thread_id}

            toolkit = MagicMock()
            toolkit.get_tools.return_value = [
                search_gmail,
                get_gmail_message,
                get_gmail_thread,
                SimpleNamespace(name="send_gmail_message"),
            ]
            with (
                patch(
                    "z_apply_core.gmail_tools.get_google_credentials",
                    return_value=SimpleNamespace(),
                ) as get_credentials,
                patch(
                    "z_apply_core.gmail_tools.build_resource_service",
                    return_value=SimpleNamespace(),
                ),
                patch("z_apply_core.gmail_tools.GmailToolkit", return_value=toolkit),
            ):
                gmail_tools = make_gmail_tools(
                    credentials_path=credentials_path,
                    token_path=token_path,
                )
                self.assertEqual(
                    [gmail_tool.name for gmail_tool in gmail_tools],
                    ["search_gmail", "get_gmail_message", "get_gmail_thread"],
                )
                get_credentials.assert_not_called()

                result = await gmail_tools[0].ainvoke(
                    {"query": "newer_than:1d", "max_results": 3}
                )

            get_credentials.assert_called_once_with(
                scopes=[READONLY_GMAIL_SCOPE],
                token_file=str(token_path.resolve()),
                client_secrets_file=str(credentials_path.resolve()),
            )
            self.assertEqual(result["query"], "newer_than:1d")
            self.assertEqual(result["resource"], "messages")
            self.assertEqual(result["max_results"], 3)

    async def test_missing_oauth_files_return_recoverable_tool_error(self) -> None:
        gmail_tools = make_gmail_tools(
            credentials_path=Path("/missing/credentials.json"),
            token_path=Path("/missing/token.json"),
        )

        result = await gmail_tools[0].ainvoke({"query": "newer_than:1d"})

        self.assertIn("human verification fallback", result)


if __name__ == "__main__":
    unittest.main()
