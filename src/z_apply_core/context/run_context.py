from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from z_apply_core.context.action_log import ActionLog

if TYPE_CHECKING:
    from z_apply_core.context.token_metric import TokenUsage


@dataclass(slots=True)
class RunContext:
    run_id: str = ""
    action_log: ActionLog = field(default_factory=ActionLog)
    token_usage: TokenUsage | None = None
    usage_totals: TokenUsage | None = None

    def note_usage(self, usage: TokenUsage) -> None:
        self.token_usage = usage
