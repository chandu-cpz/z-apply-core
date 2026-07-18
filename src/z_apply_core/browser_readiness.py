from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FormControlBlocker:
    control: str
    reasons: tuple[str, ...]

    @property
    def summary(self) -> str:
        return f"{self.control}: {', '.join(self.reasons)}"


@dataclass(frozen=True, slots=True)
class SubmitControlState:
    control: str
    disabled: bool


@dataclass(frozen=True, slots=True)
class BrowserFormReadiness:
    blockers: tuple[FormControlBlocker, ...]
    submit_controls: tuple[SubmitControlState, ...]
