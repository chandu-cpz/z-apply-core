from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BrowserControlState:
    """Exact structural state of one browser-resolved form target."""

    target: str
    value: str = ""
    has_value: bool = False
    required: bool = False
    invalid: bool = False
    disabled: bool = False


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

    def render(self) -> str:
        return "\n".join(
            (
                f"editable_controls_visible={str(self.editable_controls_visible).lower()}",
                f"unresolved_required_controls={self.unresolved_required_controls}",
                f"invalid_controls={self.invalid_controls}",
                f"auth_gate_visible={str(self.auth_gate_visible).lower()}",
                f"empty_file_upload_present={str(self.empty_file_upload_present).lower()}",
                f"required_file_upload_pending={str(self.required_file_upload_pending).lower()}",
                f"enabled_form_submit_visible={str(self.enabled_form_submit_visible).lower()}",
                f"disabled_form_submit_visible={str(self.disabled_form_submit_visible).lower()}",
                f"visual_only_surface_visible={str(self.visual_only_surface_visible).lower()}",
            )
        )


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

    def compact_render(self, *, max_chars: int = 8_000) -> str:
        """Render bounded, interaction-focused evidence for a model turn.

        The complete observation remains available from browser tools and artifacts.
        This projection only limits repeated context injection; it is not used to
        infer browser state or authorize an action.
        """
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
            "available through browser_observe/browser_snapshot]\n"
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


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """Browser-owned evidence describing one completed mutation attempt."""

    tool: str
    arguments: dict[str, Any]
    before_revision: int
    after: BrowserObservation
    changed: bool
    result: str = ""

    def render(self) -> str:
        return (
            "BROWSER ACTION RECEIPT\n"
            f"action: {self.tool}\n"
            f"arguments: {self.arguments}\n"
            f"before_revision: {self.before_revision}\n"
            f"after_revision: {self.after.revision}\n"
            f"changed: {'true' if self.changed else 'false'}\n"
            f"executor_result: {self.result or '(no separate result)'}\n"
            f"{self.after.compact_render()}"
        )


@dataclass(slots=True)
class SubmissionCapability:
    """One human authorization bound to an exact reviewed browser state."""

    run_id: str
    browser_revision: int
    page_signature: str
    target: str
    review_digest: str
    approved: bool = False
    consumed: bool = False
