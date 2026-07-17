from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock

from langchain_core.tools import ToolException

from z_apply_core.browser_tools import (
    BrowserToolRegistry,
    make_auth_submit_tool,
    make_click_upload_tool,
    make_verification_link_tool,
    validate_bounded_wait_arguments,
)


class SimpleParam:
    def __init__(
        self,
        name: str,
        annotation: Any,
        default: Any,
        description: str | None,
        hidden: bool,
    ) -> None:
        self.name = name
        self.annotation = annotation
        self.default = default
        self.description = description
        self.hidden = hidden


class SimpleSpec:
    def __init__(
        self,
        name: str,
        title: str | None,
        description: str | None,
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

    def test_aria_reference_variants_are_canonicalized(self) -> None:
        tools = self.registry.langchain_tools(["browser_snapshot"])

        for supplied in (
            "e125",
            "[e125]",
            "ref=e125",
            "[ref=e125]",
            'textbox "Current Salary *" [ref=e125]:',
        ):
            self._run(tools[0].coroutine(target=supplied))
            self.assertEqual(self.captured["target"], "e125")

    def test_fill_form_nested_targets_are_canonicalized(self) -> None:
        spec = SimpleSpec(
            name="browser_fill_form",
            title="Fill form",
            description="Fill several form controls",
            parameters=[SimpleParam("fields", list[dict[str, Any]], None, "Fields", False)],
        )
        registry = BrowserToolRegistry(specs=[spec], caller=self.registry._caller)

        self._run(
            registry.langchain_tools()[0].ainvoke(
                {
                    "fields": [
                        {
                            "name": "Current Salary *",
                            "target": 'textbox "Current Salary *" [ref=e125]:',
                            "type": "textbox",
                            "value": "600000",
                        },
                        {
                            "name": "Available To Join",
                            "target": "[e138]",
                            "type": "textbox",
                            "value": "0",
                        },
                    ]
                }
            )
        )

        self.assertEqual(
            [field["target"] for field in self.captured["fields"]],
            ["e125", "e138"],
        )

    def test_tool_model_coerces_provider_stringified_scalars(self) -> None:
        tool = self.registry.langchain_tools(["browser_snapshot"])[0]

        self._run(tool.ainvoke({"depth": "10", "boxes": "false"}))

        self.assertEqual(self.captured["depth"], 10)
        self.assertIs(self.captured["boxes"], False)

    def test_browser_error_is_returned_to_agent_for_in_loop_recovery(self) -> None:
        async def fail(_name: str, _arguments: dict[str, Any]) -> str:
            raise ToolException("stale browser ref")

        tool = _make_registry(caller=fail).langchain_tools(["browser_snapshot"])[0]

        result = self._run(tool.ainvoke({}))

        self.assertEqual(result, "stale browser ref")

    def test_atomic_click_upload_passes_typed_paths_to_direct_uploader(self) -> None:
        uploader = AsyncMock(return_value="resume attached with current form snapshot")
        tool = make_click_upload_tool(uploader)

        result = self._run(
            tool.ainvoke(
                {
                    "target": "e40",
                    "paths": '["/resume.pdf"]',
                    "element": "primary resume",
                }
            )
        )

        uploader.assert_awaited_once_with("e40", ["/resume.pdf"])
        self.assertIn("resume attached", result)
        self.assertIn("current form snapshot", result)

    def test_atomic_click_upload_uses_configured_resume_when_model_sends_empty_paths(self) -> None:
        uploader = AsyncMock(return_value="resume attached")
        tool = make_click_upload_tool(uploader, default_paths=("/resume.pdf",))

        result = self._run(tool.ainvoke({"target": "e40", "paths": []}))

        uploader.assert_awaited_once_with("e40", ["/resume.pdf"])
        self.assertEqual(result, "resume attached")

    def test_atomic_click_upload_resolves_configured_resume_basename(self) -> None:
        uploader = AsyncMock(return_value="resume attached")
        tool = make_click_upload_tool(
            uploader,
            default_paths=("/profiles/candidate/Chandrakanth-V-Resume.pdf",),
        )

        result = self._run(
            tool.ainvoke(
                {"target": "e40", "paths": ["Chandrakanth-V-Resume.pdf"]}
            )
        )

        uploader.assert_awaited_once_with(
            "e40",
            ["/profiles/candidate/Chandrakanth-V-Resume.pdf"],
        )
        self.assertEqual(result, "resume attached")

    def test_auth_submit_adapter_returns_stale_ref_failure_to_agent(self) -> None:
        submitter = AsyncMock(side_effect=ValueError("stale ref"))
        tool = make_auth_submit_tool(submitter)

        result = self._run(tool.ainvoke({"target": "e341"}))

        self.assertIn("no longer current", result)

    def test_auth_submit_adapter_accepts_provider_null_optional_element(self) -> None:
        submitter = AsyncMock(return_value="authenticated")
        tool = make_auth_submit_tool(submitter)

        result = self._run(tool.ainvoke({"target": "e341", "element": None}))

        submitter.assert_awaited_once_with("e341")
        self.assertEqual(result, "authenticated")

    def test_verification_link_tool_uses_atomic_lifecycle(self) -> None:
        opener = AsyncMock(return_value="temporary tab closed; original restored")
        tool = make_verification_link_tool(opener)

        result = self._run(tool.ainvoke({"url": "https://example.com/verify"}))

        opener.assert_awaited_once_with("https://example.com/verify")
        self.assertIn("original restored", result)

    def test_browser_wait_rejects_millisecond_shaped_duration(self) -> None:
        with self.assertRaisesRegex(ToolException, "uses seconds"):
            validate_bounded_wait_arguments({"time": 2000})

    def test_browser_wait_accepts_short_seconds_duration(self) -> None:
        self.assertEqual(validate_bounded_wait_arguments({"time": "3"})["time"], "3")


if __name__ == "__main__":
    unittest.main()
