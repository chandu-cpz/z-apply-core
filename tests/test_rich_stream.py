from __future__ import annotations

import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console

from z_apply_core.rich_stream import RichStreamRenderer
from z_apply_core.stream_events import FrameworkTraceEvent, V3RunResult


def event(event_name: str, *, name: str, **data: object) -> FrameworkTraceEvent:
    return FrameworkTraceEvent(event=event_name, name=name, data=data, raw={})


class RichStreamRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_trace_becomes_static_in_chronological_order(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=100)
        )

        with patch("z_apply_core.rich_stream.Live", return_value=MagicMock()) as live_type:
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
            await renderer.accept(
                event(
                    "agent_tool_start",
                    name="BrowserSpecialist",
                    tool_name="browser_snapshot",
                    input={},
                )
            )

        rendered = output.getvalue()
        live_kwargs = live_type.call_args.kwargs
        self.assertTrue(live_kwargs["transient"])
        self.assertEqual(live_kwargs["vertical_overflow"], "ellipsis")
        live_type.return_value.stop.assert_called_once_with()
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
