from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from z_apply_core.browser_session import BrowserSession, BrowserToolExecutionError


class BrowserUploadTests(unittest.TestCase):
    def _session(self) -> tuple[BrowserSession, AsyncMock]:
        trigger = MagicMock()
        tab = MagicMock()
        tab.resolve_target = AsyncMock(return_value=SimpleNamespace(locator=trigger))

        session = BrowserSession(
            None,
            run_id="upload-test",
            backend=SimpleNamespace(_ensure_tab=AsyncMock(return_value=tab)),
            tools=[],
            owns_backend=False,
        )
        session.call_tool = AsyncMock(return_value="current application snapshot")
        return session, tab.resolve_target

    def test_upload_resolves_visible_trigger_and_attaches_without_clicking(self) -> None:
        file_input = MagicMock()
        file_input.set_input_files = AsyncMock()
        session, resolver = self._session()

        with patch(
            "z_apply_core.browser_session.resolve_file_input",
            new=AsyncMock(return_value=file_input),
        ):
            result = asyncio.run(session.upload_files("e366", ["/resume.pdf"]))

        resolver.assert_awaited_once_with(target="e366")
        file_input.set_input_files.assert_awaited_once_with(["/resume.pdf"])
        session.call_tool.assert_awaited_once_with("browser_snapshot")
        self.assertIn("Files attached directly", result)

    def test_upload_rejects_ambiguous_trigger_without_opening_chooser(self) -> None:
        session, _resolver = self._session()

        with (
            patch(
                "z_apply_core.browser_session.resolve_file_input",
                new=AsyncMock(return_value=None),
            ),
            self.assertRaisesRegex(BrowserToolExecutionError, "never click"),
        ):
            asyncio.run(session.upload_files("e366", ["/resume.pdf"]))

        session.call_tool.assert_not_awaited()

    def test_upload_consumes_intercepted_chooser_without_resolving_trigger(self) -> None:
        chooser = SimpleNamespace(set_files=AsyncMock())
        session, resolver = self._session()
        session._pending_atomic_upload_target = "e610"
        session._pending_file_chooser = chooser

        result = asyncio.run(session.upload_files("e610", ["/resume.pdf"]))

        chooser.set_files.assert_awaited_once_with(["/resume.pdf"])
        resolver.assert_not_awaited()
        self.assertEqual(session.pending_atomic_upload_target, "")
        self.assertIn("Files attached directly", result)


class FileChooserGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_indirect_upload_click_is_intercepted_without_native_picker(self) -> None:
        listeners: dict[str, list[object]] = {}
        page = MagicMock()

        def on(event: str, callback: object) -> None:
            listeners.setdefault(event, []).append(callback)

        def remove_listener(event: str, callback: object) -> None:
            listeners[event].remove(callback)

        page.on.side_effect = on
        page.remove_listener.side_effect = remove_listener
        tab = SimpleNamespace(page=page)

        async def call_tool(_name: str, _arguments: object, *, meta: object) -> str:
            del meta
            for callback in tuple(listeners.get("filechooser", [])):
                callback(SimpleNamespace())  # type: ignore[operator]
            return "clicked"

        backend = SimpleNamespace(
            _ensure_tab=AsyncMock(return_value=tab),
            call_tool=AsyncMock(side_effect=call_tool),
        )
        session = BrowserSession(
            None,
            run_id="file-chooser-guard",
            backend=backend,
            tools=[],
            owns_backend=False,
        )
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            BrowserToolExecutionError,
            "file chooser activation intercepted",
        ):
            await session.call_tool("browser_click", {"target": "e610"})

        page.on.assert_called_once_with("filechooser", ANY)
        page.remove_listener.assert_called_once_with("filechooser", ANY)
        self.assertEqual(session.pending_atomic_upload_target, "e610")

    async def test_ordinary_click_executes_with_temporary_filechooser_listener(self) -> None:
        page = MagicMock()
        tab = SimpleNamespace(page=page)
        backend = SimpleNamespace(
            _ensure_tab=AsyncMock(return_value=tab),
            call_tool=AsyncMock(return_value="clicked"),
        )
        session = BrowserSession(
            None,
            run_id="file-chooser-guard",
            backend=backend,
            tools=[],
            owns_backend=False,
        )
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]

        result = await session.call_tool("browser_click", {"target": "e7"})

        self.assertEqual(result, "clicked")
        page.on.assert_called_once_with("filechooser", ANY)
        page.remove_listener.assert_called_once_with("filechooser", ANY)


if __name__ == "__main__":
    unittest.main()
