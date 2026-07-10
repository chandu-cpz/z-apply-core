from __future__ import annotations

import json
import unittest

from langchain_core.tools import StructuredTool

from z_apply_core.agents.browser_operation import RESUME_PATH, build_browser_operation_tool


def _tool(name: str, calls: list[tuple[str, dict[str, object]]]) -> StructuredTool:
    if name == "browser_click":
        async def run(target: str, element: str | None = None) -> str:
            arguments: dict[str, object] = {"target": target}
            if element is not None:
                arguments["element"] = element
            calls.append((name, arguments))
            return f"{name}-output"
    elif name == "browser_file_upload":
        async def run(paths: list[str] | None = None) -> str:
            calls.append((name, {"paths": paths}))
            return f"{name}-output"
    else:
        async def run() -> str:
            calls.append((name, {}))
            return f"{name}-output"

    return StructuredTool.from_function(coroutine=run, name=name, description=name)


class BrowserOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_executes_click_upload_then_snapshot(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []
        tool = build_browser_operation_tool(
            [
                _tool("browser_click", calls),
                _tool("browser_file_upload", calls),
                _tool("browser_snapshot", calls),
            ]
        )

        raw = await tool.ainvoke({"operation": "upload_resume", "target": "e40"})
        outcome = json.loads(raw)

        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(
            calls,
            [
                ("browser_click", {"target": "e40"}),
                ("browser_file_upload", {"paths": [RESUME_PATH]}),
                ("browser_snapshot", {}),
            ],
        )

    async def test_mutation_requires_snapshot_ref(self) -> None:
        tool = build_browser_operation_tool([])

        outcome = json.loads(await tool.ainvoke({"operation": "click"}))

        self.assertEqual(outcome["status"], "failed")
        self.assertIn("snapshot ref", outcome["error"])
