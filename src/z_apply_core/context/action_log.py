from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from z_apply_core.browser_observation import ActionReceipt


@dataclass(slots=True)
class ActionLog:
    entries: list[ActionReceipt] = field(default_factory=list)

    def record(self, receipt: ActionReceipt) -> None:
        self.entries.append(receipt)

    def __len__(self) -> int:
        return len(self.entries)

    def iter_entries(self) -> Iterator[ActionReceipt]:
        yield from self.entries

    def last_action(self) -> ActionReceipt | None:
        return self.entries[-1] if self.entries else None
