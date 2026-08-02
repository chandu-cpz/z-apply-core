from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from z_apply_core.agents.specialists.answer_writer import CandidateFieldAnswer


@dataclass(slots=True)
class AppliedFieldLedger:
    applied: dict[str, CandidateFieldAnswer] = field(default_factory=dict)

    def record(self, answer: CandidateFieldAnswer) -> None:
        self.applied[answer.field_label] = answer

    def lookup(self, key: str) -> CandidateFieldAnswer | None:
        return self.applied.get(key)

    def iter_fields(self) -> Iterator[CandidateFieldAnswer]:
        yield from self.applied.values()

    def as_dict(self) -> dict[str, CandidateFieldAnswer]:
        return dict(self.applied)
