from __future__ import annotations

import pytest

from z_apply_core.browser_observation import (
    DEFAULT_EVIDENCE_BUDGET_CHARS,
    BrowserObservation,
)
from z_apply_core.context.evidence_projection import EvidenceProjection

# Historical/synthetic format (ref before role) must keep classifying sanely:
# stored observations from older revisions may carry it.
EVIDENCE_LINES = [
    '[ref=nav] navigation "Site navigation"',
    '[ref=banner] banner "Brand"',
    '[ref=h1] heading "Senior Engineer Role"',
    '[ref=lbl] label "Full name"',
    '[ref=name] textbox "Full name"',
    '[ref=country] combobox "Country"',
    '[ref=cb] checkbox "I agree to the terms"',
    '[ref=apply] button "Apply Now"',
    '[ref=terms] link "Terms of service"',
    '[ref=p1] paragraph "This role requires five years of experience."',
    '[ref=o1] option "Option 1"',
    '[ref=o2] option "Option 2"',
    '[ref=o3] option "Option 3"',
    '[ref=o4] option "Option 4"',
    '[ref=o5] option "Option 5"',
    '[ref=o6] option "Option 6"',
    '[ref=o7] option "Option 7"',
    '[ref=o8] option "Option 8"',
    "[ref=sep] separator",
    '[ref=li1] listitem "Alpha"',
    '[ref=li2] listitem "Beta"',
    "Company description with no ref token",
]
EVIDENCE = "\n".join(EVIDENCE_LINES)

INTERACTIVE_LINES = [
    '[ref=name] textbox "Full name"',
    '[ref=country] combobox "Country"',
    '[ref=cb] checkbox "I agree to the terms"',
    '[ref=apply] button "Apply Now"',
    '[ref=terms] link "Terms of service"',
    '[ref=o1] option "Option 1"',
    '[ref=o2] option "Option 2"',
    '[ref=o3] option "Option 3"',
]
COLLAPSED_LINE = "[ref=o1] option x8 similar items (collapsed)"


def _observation(evidence: str = EVIDENCE, revision: int = 17) -> BrowserObservation:
    return BrowserObservation.create(
        revision=revision,
        url="https://example.test/apply",
        title="Apply",
        evidence=evidence,
    )


def _project(evidence: str, budget_chars: int | None = None) -> str:
    observation = _observation(evidence)
    if budget_chars is None:
        budget_chars = DEFAULT_EVIDENCE_BUDGET_CHARS
    return EvidenceProjection.project(observation, budget_chars=budget_chars)


def _real_page(
    *,
    filler_paragraphs: int = 0,
    next_label: str = "Next",
    apply_buttons: int = 0,
    profile_entries: int = 6,
) -> str:
    """A realistic multi-section one-page form in REAL aria-snapshot format."""
    lines = [
        "- document [ref=e1]:",
        "  - banner [ref=e11]:",
        '    - img "Company Logo" [ref=e12]',
        "  - main [ref=e23]:",
        '    - heading "Easy Apply" [level=3] [ref=e29]',
        '    - button "Choose a file or drop it here" [ref=e42] [cursor=pointer]',
    ]
    for i in range(apply_buttons):
        lines.append(f'    - button "Apply With Partner {i}" [ref=e9{i}0] [cursor=pointer]')
    ref = 100
    for entry in range(profile_entries):
        lines.extend(
            [
                f"    - generic [ref=e{ref}]:",
                f'      - heading "Experience" [level=3] [ref=e{ref + 1}]',
            ]
        )
        for field in range(4):
            lines.extend(
                [
                    f"      - generic [ref=e{ref + 2 + field * 2}]:",
                    f'        - textbox "Detail {entry}-{field}" [ref=e{ref + 3 + field * 2}]:',
                ]
            )
        lines.extend(
            [
                f'      - button "Edit experience entry {entry}" [ref=e{ref + 20}] [cursor=pointer]',
                f'      - button "Delete experience entry {entry}" [ref=e{ref + 21}] [cursor=pointer]',
            ]
        )
        ref += 40
    for _ in range(filler_paragraphs):
        lines.append(
            "    - paragraph [ref=e777]: "
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod "
            "tempor incididunt ut labore et dolore magna aliqua ut enim ad minim."
        )
    lines.extend(
        [
            '    - heading "Resume *" [level=3] [ref=e277]',
            '    - button "Choose a file or drop it here" [ref=e286] [cursor=pointer]',
            '    - textbox "Message to the Hiring Team" [ref=e303]',
            f'    - button "{next_label}" [ref=e311] [cursor=pointer]:',
            "      - generic [ref=e313]: " + next_label,
            '    - link "Privacy Notice" [ref=e316] [cursor=pointer]',
        ]
    )
    return "\n".join(lines)


def test_full_text_preserved_under_budget() -> None:
    observation = _observation()
    projected = EvidenceProjection.project(
        observation, budget_chars=len(observation.render()) + 100
    )
    assert projected == observation.render()


def test_over_budget_keeps_interactive_and_appends_marker() -> None:
    observation = _observation()
    # Budget fits exactly the interactive tier plus the marker head line.
    marker_head = "[evidence truncated to budget — "

    projected = EvidenceProjection.project(observation, budget_chars=400)

    for line in INTERACTIVE_LINES:
        assert line in projected
    assert COLLAPSED_LINE in projected
    assert "[ref=o8] option" not in projected
    option_lines = [
        line for line in projected.splitlines() if " option " in line and "[ref=" in line
    ]
    assert len(option_lines) <= 4
    assert marker_head in projected


def test_real_format_lines_classify_by_leading_role() -> None:
    from z_apply_core.context.evidence_projection import _classify_line

    assert _classify_line('- button "Next" [ref=e311] [cursor=pointer]:') == "interactive"
    assert _classify_line('  - textbox "Email" [ref=e105]:') == "interactive"
    assert _classify_line('- heading "Resume *" [level=3] [ref=e277]:') == "informative"
    assert _classify_line("  - main [ref=e23]:") == "chrome"
    assert _classify_line("- banner [ref=e11]:") == "chrome"
    assert _classify_line("- document [ref=e1]:") == "informative"


def test_truncated_long_form_keeps_advance_control() -> None:
    """Regression shape: long pre-filled profile pushes the view past the
    budget; the single advance control at the bottom must survive anyway."""
    projected = _project(_real_page(profile_entries=25))

    assert "evidence truncated to budget" in projected
    assert 'button "Next" [ref=e311]' in projected
    # Dropped informative context is named for directed captures, not lost.
    assert "- omitted section:" in projected


def test_force_include_dedupes_and_caps() -> None:
    """Interactive tier saturated by non-lexicon textboxes first: only the
    capped force-include pass may rescue lexicon controls past the budget."""
    lines = ["- document [ref=e1]:", "  - main [ref=e23]:"]
    for i in range(200):
        lines.append(f'  - textbox "Data field {i}" [ref=t{i}]:')
    for i in range(20):
        lines.append(f'  - button "Apply With Partner {i}" [ref=a{i}] [cursor=pointer]')
    projected = _project("\n".join(lines))

    assert "evidence truncated to budget" in projected
    apply_lines = [line for line in projected.splitlines() if 'button "Apply With Partner' in line]
    assert len(apply_lines) <= 8


def test_lexicon_is_floor_not_ceiling_unknown_name_still_surfaced_in_marker() -> None:
    """Advance control outside the lexicon: projection may drop it under an
    extreme budget, but the truncation marker must name it with its ref so the
    agent can direct a capture (full-page coverage path)."""
    projected = _project(_real_page(next_label="Weiter", profile_entries=60), budget_chars=900)

    assert "evidence truncated to budget" in projected
    assert "- omitted control:" in projected
    assert 'button "Weiter" [ref=e311]' in projected
    assert "- omitted section:" in projected
    # The directed-capture path: the named ref exists in the full evidence.
    full = _observation(_real_page(next_label="Weiter", profile_entries=60)).evidence
    assert "[ref=e311]" in full


def test_forced_entry_steals_chrome_before_informative() -> None:
    """Unit-level: when the budget has no room for a forced navigation
    control, capacity comes out of chrome selections first, then informative,
    and never touches interactive lines."""
    from z_apply_core.context.evidence_projection import _steal_budget

    tier_of_selected = {
        10: "chrome",
        11: "chrome",
        12: "informative",
        13: "interactive",
    }
    lengths = {10: 30, 11: 30, 12: 100, 13: 50}

    freed = _steal_budget(
        dict(tier_of_selected), lengths=lengths, wanted=("chrome", "informative"), needed=25
    )

    assert freed == 30  # only the newest chrome line, nothing else


def test_collapsed_runs_work_with_real_format() -> None:
    options = [f'  - listitem "Item {i}" [ref=l{i}]' for i in range(10)]
    evidence = "- document [ref=e1]:\n" + "\n".join(options)
    # Small budget forces the truncation path where run-collapsing applies.
    projected = _project(evidence, budget_chars=400)

    assert "listitem x10 similar items (collapsed)" in projected


@pytest.mark.parametrize("budget", [200, 555, 1200])
def test_projection_is_deterministic(budget: int) -> None:
    first = _project(_real_page(), budget_chars=budget)
    second = _project(_real_page(), budget_chars=budget)
    assert first == second


def test_empty_observation_projects_empty_string() -> None:
    observation = _observation(evidence="")
    assert EvidenceProjection.project(observation) == ""
