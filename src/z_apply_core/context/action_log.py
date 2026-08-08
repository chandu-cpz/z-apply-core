from __future__ import annotations

from dataclasses import dataclass, field

from z_apply_core.browser_observation import ActionReceipt


@dataclass(slots=True)
class ActionLog:
    entries: list[ActionReceipt] = field(default_factory=list)

    def record(self, receipt: ActionReceipt) -> None:
        self.entries.append(receipt)

    def __len__(self) -> int:
        return len(self.entries)
