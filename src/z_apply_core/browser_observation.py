from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from typing import Any

from z_apply_core.browser_value_provenance import ProvenanceEntry

# Shared evidence budget: every caller renders observations through the same
# 8k tiered projection so repeated context injection stays predictable.
# Defined here (base layer) because the projection modules sit above this one
# and a runtime import from z_apply_core.context would be circular.
DEFAULT_EVIDENCE_BUDGET_CHARS = 8000


@dataclass(frozen=True, slots=True)
class BrowserControlState:
    """Exact structural state of one browser-resolved form target."""

    value: str = ""
    has_value: bool = False
    invalid: bool = False
    disabled: bool = False
    control_name: str = ""
    role: str = ""


@dataclass(frozen=True, slots=True)
class BrowserCapabilities:
    """High-confidence structural facts used to narrow legal agent actions."""

    editable_controls_visible: bool = False
    unresolved_required_controls: int = 0
    invalid_controls: int = 0
    auth_gate_visible: bool = False
    empty_file_upload_present: bool = False
    required_file_upload_pending: bool = False
    enabled_form_submit_visible: bool = False
    disabled_form_submit_visible: bool = False
    visual_only_surface_visible: bool = False
    unresolved_names: tuple[str, ...] = ()
    # Observability: cost + coverage of the inspection that produced
    # this snapshot. Defaults keep hand-built test instances valid.
    inspection_ms: int = 0
    controls_scanned: int = 0

    def render(self, *, include_counts: bool = True) -> str:
        """Render the capability snapshot for the model context.

        ``include_counts=False`` (no-counters mode) strips aggregate
        count lines (unresolved/invalid totals) while keeping per-field rows,
        upload state, and submit visibility — an A/B arm testing whether the
        counters anchor the model on deficits instead of actionable rows.
        """
        unresolved_line = f"unresolved_required_controls={self.unresolved_required_controls}"
        if self.unresolved_names:
            named = ", ".join(f"{name}*" for name in self.unresolved_names[:3])
            unresolved_line = f"{unresolved_line} ({named})"
        lines: list[str] = [
            f"editable_controls_visible={str(self.editable_controls_visible).lower()}",
        ]
        if include_counts:
            lines.append(unresolved_line)
            lines.append(f"invalid_controls={self.invalid_controls}")
        elif self.unresolved_names:
            # no-counters keeps the per-field rows an agent can act on,
            # dropping only the aggregate numbers.
            named = ", ".join(f"{name}*" for name in self.unresolved_names[:3])
            lines.append(f"unresolved_fields: {named}")
        lines.extend(
            (
                f"auth_gate_visible={str(self.auth_gate_visible).lower()}",
                f"empty_file_upload_present={str(self.empty_file_upload_present).lower()}",
                f"required_file_upload_pending={str(self.required_file_upload_pending).lower()}",
                f"enabled_form_submit_visible={str(self.enabled_form_submit_visible).lower()}",
                f"disabled_form_submit_visible={str(self.disabled_form_submit_visible).lower()}",
                f"visual_only_surface_visible={str(self.visual_only_surface_visible).lower()}",
            )
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    """One revisioned view of the browser-owned accessibility evidence."""

    revision: int
    signature: str
    url: str
    title: str
    evidence: str

    @classmethod
    def create(
        cls,
        *,
        revision: int,
        url: str,
        title: str,
        evidence: str,
    ) -> BrowserObservation:
        signature = hashlib.sha256(
            f"{url}\0{title}\0{evidence}".encode("utf-8", errors="replace")
        ).hexdigest()
        return cls(
            revision=revision,
            signature=signature,
            url=url,
            title=title,
            evidence=evidence,
        )

    def render(self) -> str:
        return (
            "BROWSER OBSERVATION\n"
            f"revision: {self.revision}\n"
            f"signature: {self.signature[:16]}\n"
            f"url: {self.url or '(unknown)'}\n"
            f"title: {self.title or '(untitled)'}\n"
            "current accessibility evidence:\n"
            f"{self.evidence}"
        )

    def compact_render(self, *, max_chars: int = 5_000) -> str:
        """Deprecated: use :meth:`bounded_render` or :class:`EvidenceProjection`.

        Kept for backward compatibility only. New code should use
        ``bounded_render()`` (defaults to ``DEFAULT_EVIDENCE_BUDGET_CHARS``) or
        ``EvidenceStore.render_bounded`` / ``EvidenceProjection.project`` which
        share a single 8k tiered projection.

        The complete observation remains available from browser tools and artifacts.
        This projection only limits repeated context injection; it is not used to
        infer browser state or authorize an action. The 5k budget keeps every
        repeated receipt focused on controls (they are prioritized first) while
        roughly halving the context growth that makes cumulative input tokens
        balloon across a run.
        """
        warnings.warn(
            "BrowserObservation.compact_render is deprecated; use bounded_render() "
            "or EvidenceProjection.project / EvidenceStore.render_bounded",
            DeprecationWarning,
            stacklevel=2,
        )
        header = (
            "BROWSER OBSERVATION\n"
            f"revision: {self.revision}\n"
            f"signature: {self.signature[:16]}\n"
            f"url: {self.url or '(unknown)'}\n"
            f"title: {self.title or '(untitled)'}\n"
            "current accessibility evidence:\n"
        )
        if len(header) + len(self.evidence) <= max_chars:
            return header + self.evidence

        primary_roles = (
            "textbox",
            "checkbox",
            "radio",
            "combobox",
            "listbox",
            "option",
            "alert",
            "status",
            "spinbutton",
            "switch",
            "dialog",
        )
        secondary_roles = ("button", "link", "heading", "tab")
        lines = self.evidence.splitlines()
        primary: set[int] = set()
        secondary: set[int] = set(range(min(12, len(lines))))
        for index, line in enumerate(lines):
            normalized = line.casefold()
            window = range(max(0, index - 4), min(len(lines), index + 2))
            if any(role in normalized for role in primary_roles):
                primary.update(window)
            elif any(role in normalized for role in secondary_roles):
                secondary.update(window)

        marker = (
            "\n[bounded current-page view; full accessibility evidence remains "
            "available through browser_snapshot]\n"
        )
        budget = max(0, max_chars - len(header) - len(marker))
        selected: set[int] = set()
        used = 0
        for index in [*sorted(primary), *sorted(secondary - primary)]:
            line = lines[index]
            addition = len(line) + 1
            if used + addition > budget:
                continue
            selected.add(index)
            used += addition
        kept = [lines[index] for index in sorted(selected)]
        return header + "\n".join(kept) + marker

    def bounded_render(self, budget_chars: int = DEFAULT_EVIDENCE_BUDGET_CHARS) -> str:
        """Render a deterministic, budget-bounded evidence projection."""
        from z_apply_core.context.evidence_projection import EvidenceProjection

        return EvidenceProjection.project(self, budget_chars=budget_chars)


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """Browser-owned evidence describing one completed mutation attempt."""

    tool: str
    arguments: dict[str, Any]
    before_revision: int
    after: BrowserObservation
    changed: bool
    result: str = ""
    written: tuple[ProvenanceEntry, ...] = ()

    def render(self) -> str:
        written_lines = ""
        if self.written:
            provenance = "\n".join(entry.render() for entry in self.written)
            written_lines = f"written_controls (value provenance):\n{provenance}\n"
        return (
            "BROWSER ACTION RECEIPT\n"
            f"action: {self.tool}\n"
            f"arguments: {self.arguments}\n"
            f"before_revision: {self.before_revision}\n"
            f"after_revision: {self.after.revision}\n"
            f"changed: {'true' if self.changed else 'false'}\n"
            f"executor_result: {self.result or '(no separate result)'}\n"
            f"{written_lines}"
            f"{self.after.bounded_render(budget_chars=DEFAULT_EVIDENCE_BUDGET_CHARS)}"
        )
