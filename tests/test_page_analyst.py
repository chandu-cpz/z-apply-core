from __future__ import annotations

import dataclasses
import unittest

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from z_apply_core.agents.form_phase_controller import FormPhaseController
from z_apply_core.agents.specialists.page_analyst import (
    PageAnalysis,
    build_page_analyst,
    run_page_analysis,
)
from z_apply_core.context.run_context import FormPhaseTracker, RunContext
from z_apply_core.stream_events import FormPhaseEvent


class _Clock:
    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._index = 0

    def __call__(self) -> float:
        value = self._values[self._index]
        self._index += 1
        return value


class _FakeProvider:
    async def lease(self, **kwargs: object) -> object:
        raise AssertionError("lease must not be called during registration")

    def record_failure(self, model_id: str, **kwargs: object) -> None:
        pass

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        pass


class PageAnalystTests(unittest.IsolatedAsyncioTestCase):
    def test_page_analysis_is_a_dataclass_with_typed_fields(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(PageAnalysis))
        analysis = PageAnalysis(
            phase="filling",
            confidence="high",
            justification="One form field is populated.",
        )
        self.assertEqual(analysis.phase, "filling")
        self.assertEqual(analysis.confidence, "high")
        self.assertEqual(analysis.justification, "One form field is populated.")

    async def test_run_page_analysis_returns_injected_classifier_result(self) -> None:
        expected = PageAnalysis(
            phase="reviewing",
            confidence="medium",
            justification="Summary step is visible.",
        )

        async def fake_classify(evidence: str) -> PageAnalysis:
            self.assertEqual(evidence, "snapshot text")
            return expected

        result = await run_page_analysis("snapshot text", classify=fake_classify)
        self.assertIs(result, expected)

    def test_apply_analysis_transitions_valid_phase(self) -> None:
        tracker = FormPhaseTracker()
        tracker.apply_analysis("reviewing")
        self.assertEqual(tracker.phase, "reviewing")
        self.assertEqual(tracker.phase_history, ["initial"])

    def test_apply_analysis_ignores_unknown_strings(self) -> None:
        tracker = FormPhaseTracker()
        tracker.apply_analysis("mystery_phase")
        self.assertEqual(tracker.phase, "initial")
        self.assertEqual(tracker.phase_history, [])

    def test_apply_analysis_no_ops_on_same_phase(self) -> None:
        tracker = FormPhaseTracker()
        tracker.phase = "filling"
        tracker.apply_analysis("filling")
        self.assertEqual(tracker.phase, "filling")
        self.assertEqual(tracker.phase_history, [])

    async def test_page_analyst_is_registered(self) -> None:
        from z_apply_core.agents.specialists import build_specialists

        self.assertEqual(build_page_analyst()["name"], "PageAnalyst")
        model = GenericFakeChatModel(messages=iter([]))
        specialists = await build_specialists(
            _FakeProvider(),
            [],
            fallback_model=model,
        )
        names = [spec["name"] for spec in specialists]
        self.assertIn("PageAnalyst", names)


class FormPhaseControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_form_phase_transitions_and_emits_event(self) -> None:
        context = RunContext(run_id="run-1")
        emitted: list[FormPhaseEvent] = []

        async def emit(event: FormPhaseEvent) -> None:
            emitted.append(event)

        async def fake_classify(evidence: str) -> PageAnalysis:
            self.assertEqual(evidence, "evidence")
            return PageAnalysis(
                phase="filling",
                confidence="high",
                justification="A field is being filled.",
            )

        controller = FormPhaseController(classify=fake_classify)
        result = await controller.update_form_phase(context, "evidence", emit)

        self.assertIsNotNone(result)
        self.assertEqual(context.form_phase.phase, "filling")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].run_id, "run-1")
        self.assertEqual(emitted[0].phase, "filling")
        self.assertEqual(emitted[0].confidence, "high")

    async def test_update_form_phase_respects_throttle(self) -> None:
        context = RunContext()
        emitted: list[FormPhaseEvent] = []
        calls: list[str] = []

        async def emit(event: FormPhaseEvent) -> None:
            emitted.append(event)

        async def fake_classify(evidence: str) -> PageAnalysis:
            calls.append(evidence)
            return PageAnalysis(
                phase="reviewing",
                confidence="medium",
                justification="Summary step is visible.",
            )

        controller = FormPhaseController(
            classify=fake_classify,
            throttle_seconds=20.0,
            clock=_Clock([0.0, 10.0, 40.0]),
        )

        first = await controller.update_form_phase(context, "e1", emit)
        self.assertEqual(context.form_phase.phase, "reviewing")
        second = await controller.update_form_phase(context, "e2", emit)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(calls, ["e1"])
        self.assertEqual(len(emitted), 1)

    async def test_update_form_phase_bypasses_throttle_after_browser_mutation(self) -> None:
        context = RunContext()
        emitted: list[FormPhaseEvent] = []
        calls: list[str] = []

        async def emit(event: FormPhaseEvent) -> None:
            emitted.append(event)

        async def fake_classify(evidence: str) -> PageAnalysis:
            calls.append(evidence)
            phase = "verifying" if evidence == "e1" else "submitted"
            return PageAnalysis(
                phase=phase,
                confidence="high",
                justification="Verification step is visible.",
            )

        controller = FormPhaseController(
            classify=fake_classify,
            throttle_seconds=60.0,
            clock=_Clock([0.0, 5.0]),
        )

        await controller.update_form_phase(context, "e1", emit)
        await controller.update_form_phase(context, "e2", emit, browser_mutated=True)

        self.assertEqual(calls, ["e1", "e2"])
        self.assertEqual(context.form_phase.phase, "submitted")
        self.assertEqual(len(emitted), 2)

    async def test_update_form_phase_does_not_transition_on_low_confidence(self) -> None:
        context = RunContext()
        emitted: list[FormPhaseEvent] = []

        async def emit(event: FormPhaseEvent) -> None:
            emitted.append(event)

        async def fake_classify(evidence: str) -> PageAnalysis:
            return PageAnalysis(
                phase="submitted",
                confidence="low",
                justification="Page content is ambiguous.",
            )

        controller = FormPhaseController(classify=fake_classify)
        result = await controller.update_form_phase(context, "evidence", emit)

        self.assertIsNotNone(result)
        self.assertEqual(context.form_phase.phase, "initial")
        self.assertEqual(emitted, [])

    async def test_update_form_phase_does_not_emit_when_phase_is_unchanged(self) -> None:
        context = RunContext()
        emitted: list[FormPhaseEvent] = []

        async def emit(event: FormPhaseEvent) -> None:
            emitted.append(event)

        async def fake_classify(evidence: str) -> PageAnalysis:
            return PageAnalysis(
                phase="filling",
                confidence="high",
                justification="Still filling.",
            )

        controller = FormPhaseController(classify=fake_classify)
        await controller.update_form_phase(context, "evidence", emit)
        await controller.update_form_phase(context, "evidence", emit, browser_mutated=True)

        self.assertEqual(context.form_phase.phase, "filling")
        self.assertEqual(len(emitted), 1)


if __name__ == "__main__":
    unittest.main()
