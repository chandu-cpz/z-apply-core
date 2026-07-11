from __future__ import annotations

import asyncio
import unittest
from typing import Any

from z_apply_core.browser_tools import BrowserToolRegistry


class SimpleParam:
    def __init__(
        self, name: str, annotation: Any, default: Any,
        description: str | None, hidden: bool,
    ) -> None:
        self.name = name
        self.annotation = annotation
        self.default = default
        self.description = description
        self.hidden = hidden


class SimpleSpec:
    def __init__(
        self, name: str, title: str | None, description: str | None,
        parameters: list[SimpleParam],
    ) -> None:
        self.name = name
        self.title = title
        self.description = description
        self.parameters = parameters


def _make_registry(
    caller: Any = None,
) -> BrowserToolRegistry:
    spec = SimpleSpec(
        name="browser_snapshot",
        title="Snapshot",
        description="Take a snapshot",
        parameters=[
            SimpleParam("target", str, "", "Target selector", False),
            SimpleParam("filename", str, "", "Save path", False),
            SimpleParam("depth", int, 1, "Depth", False),
            SimpleParam("boxes", bool, False, "Show boxes", False),
        ],
    )
    return BrowserToolRegistry(specs=[spec], caller=caller)


class StripEmptyArgsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.captured: dict[str, Any] = {}

        async def _capture(name: str, arguments: dict[str, Any]) -> str:
            self.captured = arguments
            return "ok"

        self.registry = _make_registry(caller=_capture)

    def _run(self, coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_empty_strings_removed(self) -> None:
        tools = self.registry.langchain_tools(["browser_snapshot"])
        self._run(tools[0].coroutine(target="", filename="", depth=1, boxes=False))
        self.assertNotIn("target", self.captured)
        self.assertNotIn("filename", self.captured)
        self.assertEqual(self.captured["depth"], 1)
        self.assertFalse(self.captured["boxes"])

    def test_none_removed(self) -> None:
        tools = self.registry.langchain_tools(["browser_snapshot"])
        self._run(tools[0].coroutine(target=None, depth=1))
        self.assertNotIn("target", self.captured)
        self.assertEqual(self.captured["depth"], 1)

    def test_false_preserved(self) -> None:
        tools = self.registry.langchain_tools(["browser_snapshot"])
        self._run(tools[0].coroutine(boxes=False, depth=1))
        self.assertIn("boxes", self.captured)
        self.assertFalse(self.captured["boxes"])

    def test_zero_preserved(self) -> None:
        tools = self.registry.langchain_tools(["browser_snapshot"])
        self._run(tools[0].coroutine(depth=0))
        self.assertIn("depth", self.captured)
        self.assertEqual(self.captured["depth"], 0)

    def test_nonempty_string_preserved(self) -> None:
        tools = self.registry.langchain_tools(["browser_snapshot"])
        self._run(tools[0].coroutine(target="#btn", filename="out.png"))
        self.assertEqual(self.captured["target"], "#btn")
        self.assertEqual(self.captured["filename"], "out.png")


if __name__ == "__main__":
    unittest.main()
