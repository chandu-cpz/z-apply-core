from __future__ import annotations

import unittest
from io import StringIO

from rich.console import Console

from z_apply_core.rich_stream import RichStreamRenderer
from z_apply_core.stream_events import FrameworkTraceEvent, V3RunResult


def event(event_name: str, *, name: str, **data: object) -> FrameworkTraceEvent:
    return FrameworkTraceEvent(event=event_name, name=name, data=data, raw={})


class RichStreamRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_lifecycle_uses_logger_instead_of_direct_console_output(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=100)
        )

        with self.assertLogs("z_apply_core.rich_stream", level="INFO") as captured:
            await renderer.accept(
                event(
                    "agent_lifecycle",
                    name="BrowserSpecialist",
                    status="started",
                    path="",
                )
            )

        self.assertIn("BrowserSpecialist", captured.output[0])
        self.assertIn("started", captured.output[0])
        self.assertEqual(output.getvalue(), "")

    async def test_buffered_deltas_flush_as_static_panels_before_tools(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=100)
        )

        await renderer.accept(
            event(
                "agent_message_delta",
                name="BrowserSpecialist",
                kind="reasoning",
                delta="internal chain of thought",
            )
        )
        await renderer.accept(
            event(
                "agent_message_delta",
                name="BrowserSpecialist",
                kind="text",
                delta="Observed the application form.",
            )
        )
        # Deltas stay buffered until a stream boundary.
        self.assertEqual(output.getvalue(), "")

        await renderer.accept(
            event(
                "agent_tool_start",
                name="BrowserSpecialist",
                tool_name="browser_snapshot",
                input={},
            )
        )

        rendered = output.getvalue()
        self.assertIn("internal chain of thought", rendered)
        self.assertIn("Observed the application form.", rendered)
        self.assertIn("tool start: browser_snapshot", rendered)
        self.assertLess(
            rendered.index("internal chain of thought"),
            rendered.index("Observed the application form."),
        )
        self.assertLess(
            rendered.index("Observed the application form."),
            rendered.index("tool start: browser_snapshot"),
        )

    def test_final_result_does_not_dump_browser_snapshot(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=100)
        )

        renderer.print_result(
            V3RunResult(event_count=7, duration_ms=25),
            {
                "snapshot": "FULL DOM SNAPSHOT MUST NOT BE PRINTED",
                "orchestrator_summary": "Application prepared for review.",
                "model_id": "provider/model",
                "run_status": "completed",
            },
        )

        rendered = output.getvalue()
        self.assertIn("Application prepared for review.", rendered)
        self.assertIn("Run result: completed [provider/model]", rendered)
        self.assertNotIn("FULL DOM SNAPSHOT MUST NOT BE PRINTED", rendered)


if __name__ == "__main__":
    unittest.main()
