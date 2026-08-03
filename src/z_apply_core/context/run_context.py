from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from z_apply_core.context.action_log import ActionLog
from z_apply_core.context.ledger import AppliedFieldLedger

if TYPE_CHECKING:
    from z_apply_core.context.token_metric import TokenUsage


@dataclass(slots=True)
class RunContext:
    run_id: str = ""
    action_log: ActionLog = field(default_factory=ActionLog)
    applied_fields: AppliedFieldLedger = field(default_factory=AppliedFieldLedger)
    token_usage: TokenUsage | None = None

    def short_summary(self) -> str:
        return (
            f"actions={len(self.action_log)} "
            f"applied={len(self.applied_fields.applied)}"
        )

    def note_usage(self, usage: TokenUsage) -> None:
        self.token_usage = usage
