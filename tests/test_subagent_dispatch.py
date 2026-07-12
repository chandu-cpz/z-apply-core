from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage

from z_apply_core.agents.subagent_dispatch import SubagentDispatchMiddleware


class SubagentDispatchTests(unittest.TestCase):
    def test_converts_direct_browser_specialist_call_to_task(self) -> None:
        middleware = SubagentDispatchMiddleware(["BrowserSpecialist"])
        message = AIMessage(
            content="",
            tool_calls=[{"name": "BrowserSpecialist", "args": {}, "id": "call-1"}],
        )

        result = middleware._normalize_message(message)

        self.assertEqual(result.tool_calls[0]["name"], "task")
        self.assertEqual(result.tool_calls[0]["args"]["subagent_type"], "BrowserSpecialist")

    def test_preserves_real_tool_call(self) -> None:
        middleware = SubagentDispatchMiddleware(["BrowserSpecialist"])
        message = AIMessage(
            content="",
            tool_calls=[{"name": "ask_human", "args": {"question": "Continue?"}, "id": "call-1"}],
        )

        result = middleware._normalize_message(message)

        self.assertIs(result, message)

    def test_native_task_replaces_resume_path(self) -> None:
        middleware = SubagentDispatchMiddleware(
            ["BrowserSpecialist"],
            resume_path="/actual/resume.pdf",
        )
        message = AIMessage(
            content="",
            tool_calls=[{
                "name": "task",
                "args": {
                    "subagent_type": "BrowserSpecialist",
                    "description": "Upload RESUME_PATH to the form",
                },
                "id": "call-1",
            }],
        )

        result = middleware._normalize_message(message)

        self.assertEqual(result.tool_calls[0]["name"], "task")
        desc = result.tool_calls[0]["args"]["description"]
        self.assertIn("/actual/resume.pdf", desc)
        self.assertNotIn("RESUME_PATH", desc)

    def test_native_task_preserves_non_subagent_type(self) -> None:
        middleware = SubagentDispatchMiddleware(
            ["BrowserSpecialist"],
            resume_path="/actual/resume.pdf",
        )
        message = AIMessage(
            content="",
            tool_calls=[{
                "name": "task",
                "args": {
                    "subagent_type": "SomeOtherAgent",
                    "description": "Do RESUME_PATH work",
                },
                "id": "call-1",
            }],
        )

        result = middleware._normalize_message(message)

        self.assertEqual(result.tool_calls[0]["name"], "task")
        desc = result.tool_calls[0]["args"]["description"]
        self.assertIn("RESUME_PATH", desc)
