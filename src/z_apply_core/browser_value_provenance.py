"""Value-provenance enforcement for browser writes.

Two mechanisms:

1. **Provenance entries.** Guarded value-write paths (browser_type,
   browser_fill_form, browser_select_option — standalone or batched) record
   one :class:`ProvenanceEntry` per written control: target ref, hash of the
   written value, source tool. Entries ride on the action receipt so the
   model, the reviewer backstop, and the run's action log all see them.

2. **Bypass block.** ``browser_evaluate`` rejects script bodies that write
   form-control values directly (native-setter assignment patterns). Values
   reach form controls only through typed fill tools carrying receipts —
   or they don't get written.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProvenanceEntry:
    """One recorded form-control write."""

    target: str
    value_sha256_12: str
    source_tool: str

    def render(self) -> str:
        return f"- {self.target} sha256={self.value_sha256_12} via {self.source_tool}"


def value_digest(value: Any) -> str:
    """Stable short digest of a written value (never the value itself)."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def provenance_entries_for(
    tool: str,
    arguments: dict[str, Any],
) -> tuple[ProvenanceEntry, ...]:
    """Build provenance entries for one guarded value-write tool call."""
    if tool == "browser_type":
        target = str(arguments.get("target") or "")
        text = arguments.get("text")
        if not target or text is None:
            return ()
        return (ProvenanceEntry(target, value_digest(text), tool),)
    if tool == "browser_select_option":
        target = str(arguments.get("target") or "")
        values = arguments.get("values")
        if not target or values is None:
            return ()
        joined = "|".join(str(item) for item in values)
        return (ProvenanceEntry(target, value_digest(joined), tool),)
    if tool == "browser_fill_form":
        fields = arguments.get("fields")
        if not isinstance(fields, list):
            return ()
        entries: list[ProvenanceEntry] = []
        for field in fields:
            if not isinstance(field, dict):
                continue
            target = str(field.get("target") or "")
            if not target or "value" not in field:
                continue
            entries.append(ProvenanceEntry(target, value_digest(field["value"]), tool))
        return tuple(entries)
    return ()


# Assignment-shaped writes to live form-control state. Each pattern matches
# only when it is an assignment/setter call, never a read (`==`, `===`,
# `.value.length` are fine).
_FORM_WRITE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "direct .value assignment",
        re.compile(r"\.\s*value\s*\+?=(?![=])", re.IGNORECASE),
    ),
    (
        "direct .checked assignment",
        re.compile(r"\.\s*checked\s*\+?=(?![=])", re.IGNORECASE),
    ),
    (
        "direct .textContent/.innerText assignment",
        re.compile(r"\.\s*(?:textContent|innerText)\s*\+?=(?![=])", re.IGNORECASE),
    ),
    (
        "native prototype setter extraction",
        re.compile(
            r"getOwnPropertyDescriptor\s*\(\s*(?:HTMLInputElement|HTMLTextAreaElement"
            r"|HTMLSelectElement)",
            re.IGNORECASE,
        ),
    ),
    (
        "native value setter invocation",
        re.compile(
            r"(?:HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement)\s*\.\s*prototype"
            r"\s*(?:\[\s*['\"]value['\"]\s*\]|\.value)\s*\.\s*set\b",
            re.IGNORECASE,
        ),
    ),
    (
        "descriptor setter invocation",
        re.compile(r"\.set\s*\.\s*call\s*\(", re.IGNORECASE),
    ),
    (
        "execCommand text insertion",
        re.compile(r"execCommand\s*\(\s*['\"](?:insertText|insertHTML)", re.IGNORECASE),
    ),
    (
        "value attribute write",
        re.compile(r"setAttribute\s*\(\s*['\"]value['\"]", re.IGNORECASE),
    ),
)


def first_form_write(body: str) -> str | None:
    """Return a human-readable reason when the script writes control values."""
    if not body:
        return None
    for reason, pattern in _FORM_WRITE_PATTERNS:
        match = pattern.search(body)
        if match is not None:
            return f"{reason} (matched near {match.group(0)[:40]!r})"
    return None


EVALUATE_WRITE_REJECTION = (
    "browser_evaluate rejected: this script writes form-control values "
    "({reason}). Form-control writes go ONLY through typed fill tools "
    "(browser_type, browser_fill_form, browser_select_option, "
    "browser_click_upload) so every committed value carries a provenance "
    "receipt. Re-run the read-only parts of your script without value "
    "assignments; resolve the field through the typed path."
)
