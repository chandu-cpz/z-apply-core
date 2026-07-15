from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

BROWSER_CAPABILITY_SCRIPT = r"""() => {
    const visible = element => {
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' &&
            box.width > 0 && box.height > 0;
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
    const requiredUploadPending = controls.some(element =>
        element instanceof HTMLInputElement && element.type === 'file' &&
        element.required && element.files.length === 0
    );
    const submitControls = [...document.querySelectorAll(
        'button[type="submit"], input[type="submit"], input[type="image"], form button:not([type])'
    )].filter(visible).filter(element => !element.disabled &&
        element.getAttribute('aria-disabled') !== 'true');
    return {
        editable_controls_visible: controls.length > 0,
        auth_gate_visible: authGate,
        required_file_upload_pending: requiredUploadPending,
        enabled_form_submit_visible: submitControls.length > 0,
    };
}"""


@dataclass(frozen=True, slots=True)
class BrowserCapabilities:
    """High-confidence structural facts used to narrow legal agent actions."""

    editable_controls_visible: bool = False
    auth_gate_visible: bool = False
    required_file_upload_pending: bool = False
    enabled_form_submit_visible: bool = False

    @classmethod
    def from_browser_payload(cls, payload: Any) -> BrowserCapabilities:
        data = payload if isinstance(payload, dict) else {}
        return cls(
            editable_controls_visible=bool(data.get("editable_controls_visible")),
            auth_gate_visible=bool(data.get("auth_gate_visible")),
            required_file_upload_pending=bool(data.get("required_file_upload_pending")),
            enabled_form_submit_visible=bool(data.get("enabled_form_submit_visible")),
        )

    def render(self) -> str:
        return "\n".join(
            (
                f"editable_controls_visible={str(self.editable_controls_visible).lower()}",
                f"auth_gate_visible={str(self.auth_gate_visible).lower()}",
                "required_file_upload_pending="
                f"{str(self.required_file_upload_pending).lower()}",
                "enabled_form_submit_visible="
                f"{str(self.enabled_form_submit_visible).lower()}",
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
            f"{self.after.render()}"
        )
