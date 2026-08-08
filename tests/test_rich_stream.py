from __future__ import annotations

import unittest
from io import StringIO

from rich.console import Console

from z_apply_core.rich_stream import RichStreamRenderer
from z_apply_core.stream_events import FrameworkTraceEvent, V3RunResult


def event(event_name: str, *, name: str, **data: object) -> FrameworkTraceEvent:
    return FrameworkTraceEvent(event=event_name, name=name, data=data, raw={})


class RichStreamRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_turn_renders_ttft_duration_and_tool_count(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=100)
        )

        with self.assertLogs("z_apply_core.rich_stream", level="INFO") as captured:
            await renderer.accept(
                event(
                    "agent_turn",
                    name="orchestrator",
                    agent="orchestrator",
                    text="",
                    reasoning="",
                    tool_calls=[{"name": "browser_observe"}],
                    duration_ms=4867,
                    ttft_ms=1234,
                )
            )

        self.assertIn("orchestrator", captured.output[0])
        self.assertIn("ttft 1234ms", captured.output[0])
        self.assertIn("turn 4867ms", captured.output[0])
        self.assertIn("1 tool calls", captured.output[0])
        self.assertEqual(output.getvalue(), "")

    async def test_print_result_reports_last_agent_turn_ttft(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=100)
        )
        await renderer.accept(
            event(
                "agent_turn",
                name="orchestrator",
                agent="orchestrator",
                ttft_ms=900,
                duration_ms=2000,
            )
        )

        with self.assertLogs("z_apply_core.rich_stream", level="INFO") as captured:
            renderer.print_result(V3RunResult(event_count=1, duration_ms=5), {})

        self.assertTrue(any("last agent turn ttft: 900ms" in line for line in captured.output))

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

    async def test_projection_message_content_is_suppressed_in_favor_of_middleware_events(
        self,
    ) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=100)
        )

        await renderer.accept(
            event(
                "agent_message_delta",
                name="orchestrator",
                kind="reasoning",
                delta="internal chain of thought",
            )
        )
        await renderer.accept(
            event(
                "agent_message",
                name="orchestrator",
                reasoning="trace",
                text="Observed the application form.",
            )
        )

        # Model content renders only from the middleware model_call_content
        # event; the projection messages must stay silent to avoid duplicates.
        self.assertEqual(output.getvalue(), "")

        await renderer.accept(
            event(
                "model_call_content",
                name="orchestrator",
                role="orchestrator",
                model_id="m",
                provider="p",
                reasoning="internal chain of thought",
                text="Observed the application form.",
            )
        )
        rendered = output.getvalue()
        self.assertIn("internal chain of thought", rendered)
        self.assertIn("Observed the application form.", rendered)
        self.assertEqual(rendered.count("internal chain of thought"), 1)

    async def test_model_call_start_renders_role_model_and_prompt_preview(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=120)
        )

        await renderer.accept(
            event(
                "model_call_start",
                name="AnswerWriter",
                role="AnswerWriter",
                model_id="agnes-2.0-flash",
                provider="agnes",
                input_tokens_estimate=321,
                tool_count=12,
                prompt_preview="Fill the Skills field on the current form.",
            )
        )

        rendered = output.getvalue()
        self.assertIn("AnswerWriter", rendered)
        self.assertIn("agnes-2.0-flash", rendered)
        self.assertIn("in≈321 tok", rendered)
        self.assertIn("12 tools", rendered)
        self.assertIn("Fill the Skills field", rendered)

    async def test_model_call_content_renders_reasoning_text_and_tools(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=120)
        )

        with self.assertLogs("z_apply_core.rich_stream", level="INFO") as captured:
            await renderer.accept(
                event(
                    "model_call_content",
                    name="orchestrator",
                    role="orchestrator",
                    model_id="agnes-2.0-flash",
                    provider="agnes",
                    ttft_ms=1042,
                    duration_ms=9190,
                    input_tokens=9110,
                    output_tokens=700,
                    reasoning="check the phone field first",
                    text="The phone field is present.",
                    tool_calls=[
                        {"name": "browser_click", "args": '{"target": "e208"}', "index": 0}
                    ],
                )
            )

        rendered = output.getvalue()
        self.assertIn("orchestrator thinking", rendered)
        self.assertIn("check the phone field first", rendered)
        self.assertIn("orchestrator response", rendered)
        self.assertIn("The phone field is present.", rendered)
        self.assertIn("browser_click", rendered)
        self.assertIn("orchestrator call complete", captured.output[0])
        self.assertIn("in=9110 out=700", captured.output[0])

    async def test_model_call_metrics_is_suppressed_in_favor_of_content(self) -> None:
        output = StringIO()
        renderer = RichStreamRenderer(
            Console(file=output, color_system=None, force_terminal=False, width=120)
        )

        await renderer.accept(
            event(
                "model_call_metrics",
                name="orchestrator",
                role="orchestrator",
                model_id="agnes-2.0-flash",
                provider="agnes",
                ttft_ms=100,
                input_tokens=10,
                output_tokens=20,
            )
        )

        self.assertEqual(output.getvalue(), "")

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
