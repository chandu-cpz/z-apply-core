from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from z_apply_core.agents.specialists.page_analyst import (
    PageAnalysis,
    PageClassifier,
    run_page_analysis,
)
from z_apply_core.context.run_context import RunContext
from z_apply_core.stream_events import FormPhaseEvent

FormPhaseEmitter = Callable[[FormPhaseEvent], Awaitable[None]]

DEFAULT_THROTTLE_SECONDS = 20.0


class FormPhaseController:
    """Throttled deterministic driver that turns PageAnalyst verdicts into phases.

    Classifications run only when the last one is older than the throttle
    window, or immediately after a browser mutation. Only a high/medium
    confidence verdict that actually changes the phase transitions the tracker
    and emits a typed ``FormPhaseEvent``.
    """

    def __init__(
        self,
        *,
        classify: PageClassifier = run_page_analysis,
        throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._classify = classify
        self._throttle_seconds = throttle_seconds
        self._clock = clock
        self._last_analysis_at: float | None = None

    async def update_form_phase(
        self,
        run_context: RunContext,
        browser_evidence: str,
        emit: FormPhaseEmitter,
        *,
        browser_mutated: bool = False,
    ) -> PageAnalysis | None:
        """Classify the current page and apply the phase when it actually moves."""
        now = self._clock()
        if (
            not browser_mutated
            and self._last_analysis_at is not None
            and now - self._last_analysis_at < self._throttle_seconds
        ):
            return None
        self._last_analysis_at = now

        analysis = await self._classify(browser_evidence)
        if analysis.confidence == "low":
            return analysis

        tracker = run_context.form_phase
        previous = tracker.phase
        tracker.apply_analysis(analysis.phase)
        if tracker.phase != previous:
            await emit(
                FormPhaseEvent(
                    run_id=run_context.run_id,
                    phase=tracker.phase,
                    confidence=analysis.confidence,
                )
            )
        return analysis
