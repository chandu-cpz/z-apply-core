from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from z_apply_core.browser_observation import DEFAULT_EVIDENCE_BUDGET_CHARS
from z_apply_core.context.evidence_projection import EvidenceProjection

if TYPE_CHECKING:
    from z_apply_core.browser_observation import BrowserObservation


@dataclass(slots=True)
class EvidenceStore:
    """Revision-keyed, disk-backed store of full browser observations."""

    base_dir: Path

    def __post_init__(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, observation: BrowserObservation) -> Path:
        path = self.base_dir / f"obs_{observation.revision}.txt"
        path.write_text(observation.render(), encoding="utf-8")
        return path

    def get(self, revision: str) -> str | None:
        path = self.base_dir / f"obs_{revision}.txt"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def evict_older_than(self, max_entries: int) -> None:
        files = sorted(
            self.base_dir.glob("obs_*.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in files[max_entries:]:
            stale.unlink()


def render_bounded(
    observation: BrowserObservation,
    store: EvidenceStore,
    *,
    budget_chars: int = DEFAULT_EVIDENCE_BUDGET_CHARS,
) -> str:
    store.save(observation)
    return EvidenceProjection.project(observation, budget_chars=budget_chars)
