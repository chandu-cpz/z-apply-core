from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from z_apply_core.browser_session import BrowserSession, BrowserToolExecutionError


class BrowserUploadTests(unittest.TestCase):
    def _session(self, *, file_input: object | None) -> tuple[BrowserSession, AsyncMock, AsyncMock]:
        input_handle = file_input
        js_handle = MagicMock()
        js_handle.as_element.return_value = input_handle
        js_handle.dispose = AsyncMock()

        trigger = MagicMock()
        trigger.evaluate_handle = AsyncMock(return_value=js_handle)
        tab = MagicMock()
        tab.resolve_target = AsyncMock(return_value=SimpleNamespace(locator=trigger))

        session = object.__new__(BrowserSession)
        session._backend = SimpleNamespace(_ensure_tab=AsyncMock(return_value=tab))
        session.call_tool = AsyncMock(return_value="current application snapshot")
        return session, trigger.evaluate_handle, js_handle.dispose

    def test_upload_resolves_visible_trigger_and_attaches_without_clicking(self) -> None:
        file_input = MagicMock()
        file_input.set_input_files = AsyncMock()
        session, resolver, disposer = self._session(file_input=file_input)

        result = asyncio.run(session.upload_files("e366", ["/resume.pdf"]))

        resolver.assert_awaited_once()
        file_input.set_input_files.assert_awaited_once_with(["/resume.pdf"])
        disposer.assert_awaited_once()
        session.call_tool.assert_awaited_once_with("browser_snapshot")
        self.assertIn("Files attached directly", result)

    def test_upload_rejects_ambiguous_trigger_without_opening_chooser(self) -> None:
        session, _resolver, disposer = self._session(file_input=None)

        with self.assertRaisesRegex(BrowserToolExecutionError, "never click"):
            asyncio.run(session.upload_files("e366", ["/resume.pdf"]))

        disposer.assert_awaited_once()
        session.call_tool.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
