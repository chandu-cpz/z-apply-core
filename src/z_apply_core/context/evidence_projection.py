from __future__ import annotations

import re
from typing import TYPE_CHECKING

from z_apply_core.browser_observation import DEFAULT_EVIDENCE_BUDGET_CHARS
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
_KNOWN_ROLES = _INTERACTIVE_ROLES | _INFORMATIVE_ROLES | _CHROME_ROLES
_COLLAPSED_RUN_ROLES = frozenset({"listitem", "option"})
_COLLAPSED_RUN_MIN = 6
_COLLAPSED_RUN_KEEP = 3

# Generic form-navigation vocabulary. This is wizard/one-pager vocabulary any
# job board (or any multi-section web form) uses for forward/backward motion;
# it names no board. Used two ways: as a floor guarantee in the projection
# (these controls survive truncation) and, in agent policy, as the starting
# ladder for browser_find probes — never as an exhaustive list.
NAVIGATION_LEXICON = frozenset(
    {
        "next",
        "continue",
        "back",
        "previous",
        "submit",
        "apply",
        "send",
        "review",
        "confirm",
        "done",
        "proceed",
    }
)
# Caps for the force-include pass so pathological pages (listing screens with
# dozens of "apply" buttons) cannot blow the projection up. Forced entries
# dedupe by accessible name and steal budget from the chrome tier first, then
# informative — interactive context is squeezed last.
_FORCE_INCLUDE_MAX_CONTROLS = 8
_FORCE_INCLUDE_MAX_CHARS = 600
# Truncation-marker budgets: how many dropped actionable controls / section
# headings get named explicitly so the agent can direct targeted captures at
# what the bounded view could not carry.
_MARKER_MAX_CONTROLS = 8
_MARKER_MAX_HEADINGS = 6
_MARKER_MAX_CHARS = 1200

_QUOTED_NAME_RE = re.compile(r'"([^"]*)"')


class EvidenceProjection:
    """Deterministic, budget-bounded projection of one browser observation."""

    @staticmethod
    def project(
        observation: BrowserObservation,
        *,
        budget_chars: int = DEFAULT_EVIDENCE_BUDGET_CHARS,
    ) -> str:
        if not observation.evidence.strip():
            return ""
        text = observation.render()
        if len(text) <= budget_chars:
            return text

        lines = _collapse_repetitive_runs(text.splitlines())
        classified = [(index, line, _classify_line(line)) for index, line in enumerate(lines)]
        lengths = {index: len(line) + 1 for index, line, _ in classified}
        # Which tier pulled each selected line in, so forced navigation
        # controls know whose budget they may steal.
        tier_of_selected: dict[int, str] = {}
        used = 0
        for tier in ("interactive", "informative", "chrome"):
            for index, line, tier_of in classified:
                if tier_of != tier or index in tier_of_selected:
                    continue
                addition = len(line) + 1
                if used + addition > budget_chars:
                    continue
                tier_of_selected[index] = tier
                used += addition

        used = _force_include_navigation(
            classified,
            tier_of_selected,
            used,
            budget_chars,
            lengths,
        )
        kept = [lines[index] for index in sorted(tier_of_selected)]
        marker = _truncation_marker(classified, tier_of_selected)
        return "\n".join(kept) + "\n" + "\n".join(marker)


def _force_include_navigation(
    classified: list[tuple[int, str, str]],
    tier_of_selected: dict[int, str],
    used: int,
    budget_chars: int,
    lengths: dict[int, int],
) -> int:
    """Guarantee navigation-lexicon controls survive truncation.

    Walks every interactive line (kept or dropped), dedupes by accessible
    name, and force-selects matches until the control/char caps are hit. When
    the budget has no room, freed capacity comes from chrome-tier selections
    first, then informative — never from interactive lines.
    """
    seen_names: set[str] = set()
    forced_controls = 0
    forced_chars = 0
    for index, line, tier_of in classified:
        if tier_of != "interactive":
            continue
        if index in tier_of_selected:
            name = _accessible_name(line)
            if name:
                seen_names.add(name.casefold())
            continue
        if forced_controls >= _FORCE_INCLUDE_MAX_CONTROLS:
            break
        name = _accessible_name(line)
        if not name or name.casefold() in seen_names:
            continue
        if not _matches_lexicon(name):
            continue
        addition = len(line) + 1
        if used + addition > budget_chars or forced_chars + addition > _FORCE_INCLUDE_MAX_CHARS:
            freed = _steal_budget(
                tier_of_selected,
                lengths=lengths,
                wanted=("chrome", "informative"),
                needed=min(addition, max(0, used + addition - budget_chars)),
            )
            used -= freed
            if used + addition > budget_chars:
                continue
        tier_of_selected[index] = "interactive"
        used += addition
        seen_names.add(name.casefold())
        forced_controls += 1
        forced_chars += addition
    return used


def _steal_budget(
    tier_of_selected: dict[int, str],
    *,
    lengths: dict[int, int],
    wanted: tuple[str, ...],
    needed: int,
) -> int:
    """Drop low-tier selections (latest first) until ``needed`` chars are freed."""
    freed = 0
    for tier in wanted:
        candidates = [
            index
            for index, selected_tier in reversed(list(tier_of_selected.items()))
            if selected_tier == tier
        ]
        for index in candidates:
            if freed >= needed:
                return freed
            freed += lengths.get(index, 0)
            del tier_of_selected[index]
    return freed


def _accessible_name(line: str) -> str | None:
    match = _QUOTED_NAME_RE.search(line)
    return match.group(1) if match else None


def _matches_lexicon(name: str) -> bool:
    words = re.split(r"[^a-z0-9]+", name.casefold())
    return bool(set(words) & NAVIGATION_LEXICON)


def _truncation_marker(
    classified: list[tuple[int, str, str]],
    tier_of_selected: dict[int, str],
) -> list[str]:
    """Explain WHAT was cut and hand back pointers for directed captures.

    Names up to a cap of dropped actionable controls (refs let the agent act
    or scoped-snapshot directly) and dropped section headings (chunk anchors
    for a full-coverage walk). Lists run bottom-of-page first: forms put
    advance controls and final sections at the end, so a capped marker must
    spend its budget there. This is the mechanism behind the full-page
    coverage rule: a truncated view must never leave the agent blind to the
    existence of controls it cannot see.
    """
    omitted = [entry for entry in classified if entry[0] not in tier_of_selected]
    dropped_controls = [
        (index, line) for index, line, tier_of in reversed(omitted) if tier_of == "interactive"
    ]
    dropped_headings = [
        (index, line)
        for index, line, tier_of in reversed(omitted)
        if _role_token(line) == "heading"
    ]
    out = [
        f"[evidence truncated to budget — {len(dropped_controls)} actionable control(s) "
        f"and {len(dropped_headings)} section heading(s) below are OMITTED from this view. "
        "Act on listed refs directly, scope subtree snapshots with browser_snapshot "
        "target=<ref>, or use browser_find for precise lookup.]"
    ]
    chars = len(out[0])
    for _, line in dropped_controls[:_MARKER_MAX_CONTROLS]:
        entry = f"- omitted control: {_compact(line)}"
        if chars + len(entry) > _MARKER_MAX_CHARS:
            break
        out.append(entry)
        chars += len(entry) + 1
    for _, line in dropped_headings[:_MARKER_MAX_HEADINGS]:
        entry = f"- omitted section: {_compact(line)}"
        if chars + len(entry) > _MARKER_MAX_CHARS:
            break
        out.append(entry)
        chars += len(entry) + 1
    return out


def _compact(line: str) -> str:
    stripped = " ".join(line.split())
    return stripped[:200]


def _classify_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return "chrome"
    role = _role_token(line)
    if role in _CHROME_ROLES:
        return "chrome"
    if role in _INTERACTIVE_ROLES:
        return "interactive"
    if role is None:
        first = stripped.split(" ", 1)[0].casefold()
        if first in _CHROME_ROLES or (stripped.startswith("<") and stripped.endswith(">")):
            return "chrome"
        return "informative"
    return "informative"


def _role_token(line: str) -> str | None:
    """ARIA role of a snapshot line, parsed from the REAL Playwright format.

    Real aria-snapshot lines put the role FIRST, before the ref tag::

        - button "Next" [ref=e311] [cursor=pointer]:

    So the role is the first token after the leading dashes. Historical/
    synthetic lines may carry the ref before the role (``[ref=x] button "..."``);
    for those, fall back to the first known-role token after the ref tag so
    mixed-format observations still classify sanely.
    """
    stripped = line.strip()
    if not stripped:
        return None
    lead = stripped.lstrip("-").strip()
    first = lead.split(" ", 1)[0].rstrip(":").casefold() if lead else ""
    if first in _KNOWN_ROLES:
        return first
    match = REF_TAG_RE.search(line)
    if match is not None:
        remainder = line[match.end() :].strip()
        for token in remainder.replace("]", " ]").split():
            candidate = token.strip("]:").casefold()
            if candidate in _KNOWN_ROLES:
                return candidate
    return None


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
                collapsed.append(f"[ref={ref}] {role} x{len(run)} similar items (collapsed)")
            else:
                collapsed.extend(run)
            index = end
        else:
            collapsed.append(line)
            index += 1
    return collapsed


def _ref_of(line: str) -> str | None:
    match = REF_TAG_RE.search(line)
    return match.group(1) if match is not None else None


def _run_pattern(line: str) -> str:
    text = REF_TAG_RE.sub("", line)
    quote = text.find('"')
    if quote != -1:
        text = text[:quote]
    return text.strip()
