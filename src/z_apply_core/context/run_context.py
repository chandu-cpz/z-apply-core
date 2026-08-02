from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from z_apply_core.context.action_log import ActionLog
from z_apply_core.context.ledger import AppliedFieldLedger

if TYPE_CHECKING:
    from z_apply_core.context.token_metric import TokenUsage


@dataclass(slots=True)
class FormPhaseTracker:
    INITIAL = "initial"
    FILLING = "filling"
    REVIEWING = "reviewing"
    VERIFYING = "verifying"
    SUBMITTED = "submitted"

    phase: str = "initial"
    phase_history: list[str] = field(default_factory=list)

    def transition(self, phase: str) -> None:
        if phase == self.phase:
            return
        self.phase_history.append(self.phase)
        self.phase = phase

    def apply_analysis(self, analysis_phase: str) -> None:
        if analysis_phase not in _CANONICAL_PHASES:
            return
        self.transition(analysis_phase)


_CANONICAL_PHASES = frozenset(
    {
        FormPhaseTracker.INITIAL,
        FormPhaseTracker.FILLING,
        FormPhaseTracker.REVIEWING,
        FormPhaseTracker.VERIFYING,
        FormPhaseTracker.SUBMITTED,
    }
)


@dataclass(slots=True)
class RunContext:
    run_id: str = ""
    action_log: ActionLog = field(default_factory=ActionLog)
    applied_fields: AppliedFieldLedger = field(default_factory=AppliedFieldLedger)
    form_phase: FormPhaseTracker = field(default_factory=FormPhaseTracker)
    token_usage: TokenUsage | None = None

    def short_summary(self) -> str:
        return (
            f"actions={len(self.action_log)} "
            f"applied={len(self.applied_fields.applied)} "
            f"phase={self.form_phase.phase}"
        )

    def note_usage(self, usage: TokenUsage) -> None:
        self.token_usage = usage
