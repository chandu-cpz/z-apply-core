from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

BROWSER_CAPABILITY_SCRIPT = r"""() => {
    const visible = element => {
        if (element instanceof HTMLInputElement && element.type === 'hidden') return false;
        if (element.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' &&
            Number(style.opacity || 1) > 0 && box.width > 0 && box.height > 0;
    };
    const controls = [...document.querySelectorAll(
        'input, select, textarea, [contenteditable="true"], [role="combobox"]'
    )].filter(visible).filter(element => !element.disabled);
    const authGate = controls.some(element => {
        if (!(element instanceof HTMLInputElement)) return false;
        const type = (element.type || 'text').toLowerCase();
        const autocomplete = (element.autocomplete || '').toLowerCase();
        return type === 'password' || autocomplete === 'current-password' ||
            autocomplete === 'new-password' || autocomplete === 'one-time-code';
    });
    const fileInputs = [...document.querySelectorAll('input[type="file"]')]
        .filter(visible)
        .filter(element => !element.disabled);
    const emptyFileUploadPresent = fileInputs.some(element => element.files.length === 0);
    const requiredUploadPending = fileInputs.some(element =>
        (element.required || element.getAttribute('aria-required') === 'true') &&
        element.files.length === 0
    );
    const emptyValue = element => {
        if (element instanceof HTMLInputElement) {
            if (element.type === 'file') return element.files.length === 0;
            if (element.type === 'checkbox' || element.type === 'radio') {
                return !element.checked;
            }
        }
        return !String(element.value ?? element.textContent ?? '').trim();
    };
    const requiredControl = element => element.matches(':required') ||
        element.getAttribute('aria-required') === 'true';
    const unresolvedRequiredControls = controls.filter(element =>
        requiredControl(element) && emptyValue(element)
    );
    const invalidControls = controls.filter(element =>
        element.getAttribute('aria-invalid') === 'true' ||
        ('validity' in element && !element.validity.valid)
    );
    const allSubmitControls = [...document.querySelectorAll(
        'button[type="submit"], input[type="submit"], input[type="image"], form button:not([type])'
    )].filter(visible);
    const submitControls = allSubmitControls.filter(element => !element.disabled &&
        element.getAttribute('aria-disabled') !== 'true');
    const disabledSubmitControls = allSubmitControls.filter(element => element.disabled ||
        element.getAttribute('aria-disabled') === 'true');
    const actionControls = [...document.querySelectorAll(
        'button, a[href], input, select, textarea, [role="button"], [role="link"], [role="combobox"]'
    )].filter(visible).filter(element => !element.disabled &&
        element.getAttribute('aria-disabled') !== 'true');
    const minimumVisualArea = Math.max(40000, innerWidth * innerHeight * 0.2);
    const largeVisualSurface = [...document.querySelectorAll('canvas, video, iframe, img')]
        .filter(visible).some(element => {
            const box = element.getBoundingClientRect();
            return box.width * box.height >= minimumVisualArea;
        });
    return {
        editable_controls_visible: controls.length > 0,
        unresolved_required_controls: unresolvedRequiredControls.length,
        invalid_controls: invalidControls.length,
        auth_gate_visible: authGate,
        empty_file_upload_present: emptyFileUploadPresent,
        required_file_upload_pending: requiredUploadPending,
        enabled_form_submit_visible: submitControls.length > 0,
        disabled_form_submit_visible: disabledSubmitControls.length > 0,
        visual_only_surface_visible: largeVisualSurface && actionControls.length === 0,
    };
}"""


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

    @classmethod
    def from_browser_payload(cls, payload: Any) -> BrowserCapabilities:
        data = payload if isinstance(payload, dict) else {}
        return cls(
            editable_controls_visible=bool(data.get("editable_controls_visible")),
            unresolved_required_controls=int(
                data.get("unresolved_required_controls") or 0
            ),
            invalid_controls=int(data.get("invalid_controls") or 0),
            auth_gate_visible=bool(data.get("auth_gate_visible")),
            empty_file_upload_present=bool(data.get("empty_file_upload_present")),
            required_file_upload_pending=bool(data.get("required_file_upload_pending")),
            enabled_form_submit_visible=bool(data.get("enabled_form_submit_visible")),
            disabled_form_submit_visible=bool(
                data.get("disabled_form_submit_visible")
            ),
            visual_only_surface_visible=bool(data.get("visual_only_surface_visible")),
        )

    def render(self) -> str:
        return "\n".join(
            (
                f"editable_controls_visible={str(self.editable_controls_visible).lower()}",
                f"unresolved_required_controls={self.unresolved_required_controls}",
                f"invalid_controls={self.invalid_controls}",
                f"auth_gate_visible={str(self.auth_gate_visible).lower()}",
                "empty_file_upload_present="
                f"{str(self.empty_file_upload_present).lower()}",
                "required_file_upload_pending="
                f"{str(self.required_file_upload_pending).lower()}",
                "enabled_form_submit_visible="
                f"{str(self.enabled_form_submit_visible).lower()}",
                "disabled_form_submit_visible="
                f"{str(self.disabled_form_submit_visible).lower()}",
                "visual_only_surface_visible="
                f"{str(self.visual_only_surface_visible).lower()}",
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

        roles = (
            "textbox",
            "button",
            "checkbox",
            "radio",
            "combobox",
            "listbox",
            "option",
            "link",
            "alert",
            "status",
            "heading",
            "spinbutton",
            "switch",
            "tab",
            "dialog",
        )
        lines = self.evidence.splitlines()
        selected: set[int] = set(range(min(12, len(lines))))
        for index, line in enumerate(lines):
            normalized = line.casefold()
            if any(role in normalized for role in roles):
                selected.update(range(max(0, index - 4), min(len(lines), index + 2)))

        marker = (
            "\n[bounded current-page view; full accessibility evidence remains "
            "available through browser_observe/browser_snapshot]\n"
        )
        budget = max(0, max_chars - len(header) - len(marker))
        kept: list[str] = []
        used = 0
        for index in sorted(selected):
            line = lines[index]
            addition = len(line) + 1
            if used + addition > budget:
                break
            kept.append(line)
            used += addition
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
