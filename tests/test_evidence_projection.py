from __future__ import annotations

from z_apply_core.browser_observation import BrowserObservation
from z_apply_core.context.evidence_projection import EvidenceProjection

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

MARKER = (
    "[evidence truncated to budget — full ARIA snapshot saved under revision "
    "17 in the evidence store; use browser_find for precise lookup]"
)


def _observation(evidence: str = EVIDENCE) -> BrowserObservation:
    return BrowserObservation.create(
        revision=17,
        url="https://example.test/apply",
        title="Apply",
        evidence=evidence,
    )


def test_full_text_preserved_under_budget() -> None:
    observation = _observation()
    projected = EvidenceProjection.project(
        observation, budget_chars=len(observation.render()) + 100
    )
    assert projected == observation.render()


def test_over_budget_keeps_interactive_and_appends_marker() -> None:
    observation = _observation()
    budget = sum(len(line) + 1 for line in INTERACTIVE_LINES) + len(MARKER)

    projected = EvidenceProjection.project(observation, budget_chars=budget)

    assert projected.endswith(MARKER)
    for line in INTERACTIVE_LINES:
        assert line in projected
    assert COLLAPSED_LINE in projected
    assert "[ref=o8] option" not in projected
    option_lines = [
        line for line in projected.splitlines() if " option " in line and "[ref=" in line
    ]
    assert len(option_lines) <= 4


def test_empty_observation_projects_empty_string() -> None:
    observation = _observation(evidence="")
    assert EvidenceProjection.project(observation) == ""


def test_projection_is_deterministic() -> None:
    observation = _observation()
    first = EvidenceProjection.project(observation, budget_chars=400)
    second = EvidenceProjection.project(observation, budget_chars=400)
    assert first == second
