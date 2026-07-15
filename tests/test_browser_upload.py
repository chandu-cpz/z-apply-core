from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

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
        session = object.__new__(BrowserSession)
        session._backend = backend
        session._mutation_gate = None
        session._lease = None
        session._submission_guard_active = False
        session._capture_workspace = Path("/tmp/file-chooser-guard")
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            BrowserToolExecutionError,
            "file chooser activation intercepted",
        ):
            await session.call_tool("browser_click", {"target": "e610"})

        page.on.assert_called_once_with("filechooser", ANY)
        page.remove_listener.assert_called_once_with("filechooser", ANY)

    async def test_ordinary_click_executes_with_temporary_filechooser_listener(self) -> None:
        page = MagicMock()
        tab = SimpleNamespace(page=page)
        backend = SimpleNamespace(
            _ensure_tab=AsyncMock(return_value=tab),
            call_tool=AsyncMock(return_value="clicked"),
        )
        session = object.__new__(BrowserSession)
        session._backend = backend
        session._mutation_gate = None
        session._lease = None
        session._submission_guard_active = False
        session._capture_workspace = Path("/tmp/file-chooser-guard")
        session._is_file_upload_trigger = AsyncMock(return_value=False)  # type: ignore[method-assign]

        result = await session.call_tool("browser_click", {"target": "e7"})

        self.assertEqual(result, "clicked")
        page.on.assert_called_once_with("filechooser", ANY)
        page.remove_listener.assert_called_once_with("filechooser", ANY)


if __name__ == "__main__":
    unittest.main()
