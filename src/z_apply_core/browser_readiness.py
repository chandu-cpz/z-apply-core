from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FORM_READINESS_SCRIPT = r"""() => {
    const visible = element => {
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' &&
            box.width > 0 && box.height > 0;
    };
    const controlName = element => {
        const labelledBy = (element.getAttribute('aria-labelledby') || '')
            .split(/\s+/).filter(Boolean)
            .map(id => element.ownerDocument.getElementById(id)?.textContent?.trim())
            .filter(Boolean).join(' ');
        const labels = element.labels ? [...element.labels]
            .map(label => label.textContent?.trim()).filter(Boolean).join(' ') : '';
        return labelledBy || element.getAttribute('aria-label') || labels ||
            element.getAttribute('name') || element.getAttribute('placeholder') ||
            element.id || element.tagName.toLowerCase();
    };
    const empty = element => {
        if (element instanceof HTMLInputElement &&
            ['checkbox', 'radio'].includes(element.type)) return !element.checked;
        if (element instanceof HTMLSelectElement) return !element.value;
        if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)
            return !element.value.trim();
        return !(element.textContent || '').trim();
    };

    const blockers = [];
    const controls = [...document.querySelectorAll(
        'input, select, textarea, [contenteditable="true"], [role="combobox"]'
    )].filter(visible).filter(element => !element.disabled);
    for (const element of controls) {
        const reasons = [];
        const required = element.required || element.getAttribute('aria-required') === 'true';
        if (required && empty(element)) reasons.push('required control is empty');
        if (element.getAttribute('aria-invalid') === 'true')
            reasons.push('control is marked aria-invalid');
        if (typeof element.checkValidity === 'function' && !element.checkValidity()) {
            reasons.push(element.validationMessage || 'native constraint validation failed');
        }
        const describedBy = (element.getAttribute('aria-describedby') || '')
            .split(/\s+/).filter(Boolean)
            .map(id => element.ownerDocument.getElementById(id))
            .filter(node => node && visible(node) && (node.textContent || '').trim())
            .map(node => node.textContent.trim());
        if (reasons.length) reasons.push(...describedBy);
        if (reasons.length) blockers.push({
            control: controlName(element),
            reasons: [...new Set(reasons)],
        });
    }

    const submitControls = [...document.querySelectorAll(
        'button[type="submit"], input[type="submit"], input[type="image"], form button:not([type])'
    )].filter(visible).map(element => ({
        control: controlName(element),
        disabled: Boolean(element.disabled) || element.getAttribute('aria-disabled') === 'true',
    }));
    return { blockers, submit_controls: submitControls };
}"""

REQUIRED_FILE_UPLOAD_PENDING_SCRIPT = r"""() =>
    [...document.querySelectorAll('input[type="file"]')].some(input =>
        !input.disabled && input.required && input.files.length === 0
    )"""


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

    @classmethod
    def from_browser_payload(cls, payload: Any) -> BrowserFormReadiness:
        data = payload if isinstance(payload, dict) else {}
        blockers = tuple(
            FormControlBlocker(
                control=str(item.get("control") or "unnamed control"),
                reasons=tuple(str(reason) for reason in item.get("reasons", ()) if reason),
            )
            for item in data.get("blockers", ())
            if isinstance(item, dict) and item.get("reasons")
        )
        submit_controls = tuple(
            SubmitControlState(
                control=str(item.get("control") or "submit control"),
                disabled=bool(item.get("disabled")),
            )
            for item in data.get("submit_controls", ())
            if isinstance(item, dict)
        )
        return cls(blockers=blockers, submit_controls=submit_controls)
