from __future__ import annotations

from z_apply_core.agents.specialists.answer_writer import CandidateFieldAnswer
from z_apply_core.context.ledger import AppliedFieldLedger
from z_apply_core.context.run_context import FormPhaseTracker, RunContext


def _answer(*, field_label: str, value: str, target: str = "e1") -> CandidateFieldAnswer:
    return CandidateFieldAnswer(
        source="resume",
        field_label=field_label,
        target=target,
        value=value,
    )


def test_record_and_lookup_round_trip() -> None:
    ledger = AppliedFieldLedger()
    answer = _answer(field_label="Full name", value="Chandrakanth")
    ledger.record(answer)
    assert ledger.lookup("Full name") is answer


def test_lookup_missing_key_returns_none() -> None:
    assert AppliedFieldLedger().lookup("missing") is None


def test_record_overwrites_same_key() -> None:
    ledger = AppliedFieldLedger()
    ledger.record(_answer(field_label="Email", value="first@example.com"))
    replacement = _answer(field_label="Email", value="second@example.com")
    ledger.record(replacement)
    assert ledger.lookup("Email") is replacement
    assert len(list(ledger.iter_fields())) == 1


def test_as_dict_returns_shallow_copy() -> None:
    ledger = AppliedFieldLedger()
    ledger.record(_answer(field_label="Name", value="Chandrakanth"))
    snapshot = ledger.as_dict()
    snapshot["Name"] = _answer(field_label="Name", value="Tampered")
    assert ledger.lookup("Name") is not None
    assert ledger.lookup("Name").value == "Chandrakanth"


def test_transition_records_only_real_transitions() -> None:
    tracker = FormPhaseTracker()
    tracker.transition("initial")
    assert tracker.phase == "initial"
    assert tracker.phase_history == []
    tracker.transition(FormPhaseTracker.FILLING)
    tracker.transition("filling")
    assert tracker.phase == FormPhaseTracker.FILLING
    assert tracker.phase_history == [FormPhaseTracker.INITIAL]
    tracker.transition(FormPhaseTracker.REVIEWING)
    tracker.transition(FormPhaseTracker.SUBMITTED)
    assert tracker.phase == FormPhaseTracker.SUBMITTED
    assert tracker.phase_history == [
        FormPhaseTracker.INITIAL,
        FormPhaseTracker.FILLING,
        FormPhaseTracker.REVIEWING,
    ]


def test_short_summary_formats_counts_and_phase() -> None:
    context = RunContext()
    assert context.short_summary() == "actions=0 applied=0 phase=initial"
    context.form_phase.transition(FormPhaseTracker.FILLING)
    assert "phase=filling" in context.short_summary()


def test_short_summary_does_not_leak_field_values() -> None:
    context = RunContext()
    secret = "super-secret-phone-555"
    context.applied_fields.record(_answer(field_label="Phone", value=secret))
    summary = context.short_summary()
    assert secret not in summary
    assert "Phone" not in summary
