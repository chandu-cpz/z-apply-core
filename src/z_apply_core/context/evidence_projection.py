from __future__ import annotations

from typing import TYPE_CHECKING

from z_apply_core.browser_tools import REF_TAG_RE

if TYPE_CHECKING:
    from z_apply_core.browser_observation import BrowserObservation

_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "textbox",
        "combobox",
        "checkbox",
        "radio",
        "link",
        "option",
        "menuitem",
        "tab",
        "img",
        "input",
    }
)
_INFORMATIVE_ROLES = frozenset(
    {
        "heading",
        "paragraph",
        "label",
        "separator",
        "list",
        "listitem",
        "group",
        "table",
        "cell",
    }
)
_CHROME_ROLES = frozenset(
    {
        "banner",
        "navigation",
        "contentinfo",
        "main",
        "complementary",
        "form",
    }
)
_COLLAPSED_RUN_ROLES = frozenset({"listitem", "option"})
_COLLAPSED_RUN_MIN = 6
_COLLAPSED_RUN_KEEP = 3



class EvidenceProjection:
    """Deterministic, budget-bounded projection of one browser observation."""

    @staticmethod
    def project(
        observation: BrowserObservation,
        *,
        budget_chars: int = 8000,
    ) -> str:
        if not observation.evidence.strip():
            return ""
        text = observation.render()
        if len(text) <= budget_chars:
            return text

        lines = _collapse_repetitive_runs(text.splitlines())
        classified = [
            (index, line, _classify_line(line)) for index, line in enumerate(lines)
        ]
        selected: set[int] = set()
        used = 0
        for tier in ("interactive", "informative", "chrome"):
            for index, line, tier_of in classified:
                if tier_of != tier:
                    continue
                addition = len(line) + 1
                if used + addition > budget_chars:
                    continue
                selected.add(index)
                used += addition
        kept = [lines[index] for index in sorted(selected)]
        marker = (
            "[evidence truncated to budget — full ARIA snapshot saved under revision "
            f"{observation.revision} in the evidence store; use browser_find for "
            "precise lookup]"
        )
        return "\n".join(kept) + "\n" + marker


def _classify_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return "chrome"
    role = _role_token(line)
    if role is None:
        first = stripped.split(" ", 1)[0].casefold()
        if first in _CHROME_ROLES or (stripped.startswith("<") and stripped.endswith(">")):
            return "chrome"
        return "informative"
    if role in _CHROME_ROLES:
        return "chrome"
    if role in _INTERACTIVE_ROLES:
        return "interactive"
    return "informative"


def _collapse_repetitive_runs(lines: list[str]) -> list[str]:
    collapsed: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        role = _role_token(line)
        if role in _COLLAPSED_RUN_ROLES:
            pattern = _run_pattern(line)
            end = index
            while (
                end < total
                and _role_token(lines[end]) in _COLLAPSED_RUN_ROLES
                and _run_pattern(lines[end]) == pattern
            ):
                end += 1
            run = lines[index:end]
            if len(run) > _COLLAPSED_RUN_MIN:
                collapsed.extend(run[:_COLLAPSED_RUN_KEEP])
                ref = _ref_of(run[0])
                collapsed.append(
                    f"[ref={ref}] {role} x{len(run)} similar items (collapsed)"
                )
            else:
                collapsed.extend(run)
            index = end
        else:
            collapsed.append(line)
            index += 1
    return collapsed


def _role_token(line: str) -> str | None:
    match = REF_TAG_RE.search(line)
    if match is None:
        return None
    remainder = line[match.end() :].strip()
    if not remainder:
        return None
    return remainder.split(" ", 1)[0].casefold()


def _ref_of(line: str) -> str | None:
    match = REF_TAG_RE.search(line)
    return match.group(1) if match is not None else None


def _run_pattern(line: str) -> str:
    text = REF_TAG_RE.sub("", line)
    quote = text.find('"')
    if quote != -1:
        text = text[:quote]
    return text.strip()
