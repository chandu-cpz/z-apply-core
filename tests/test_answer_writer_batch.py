from __future__ import annotations

import pytest
from pydantic import ValidationError

from z_apply_core.agents.specialists.answer_writer import (
    CandidateFieldAnswer,
    CandidateFieldAnswers,
)


def test_batch_accepts_one_or_more_resolved_fields() -> None:
    batch = CandidateFieldAnswers.model_validate(
        {
            "answers": [
                {
                    "source": "resume",
                    "field_label": "First Name",
                    "target": "e70v22",
                    "value": "Chandrakanth",
                },
                {
                    "source": "human",
                    "field_label": "Expected Salary",
                    "target": "e120",
                    "value": "600000",
                },
            ]
        }
    )

    assert len(batch.answers) == 2
    assert batch.answers[0].value == "Chandrakanth"
    assert batch.answers[1].source == "human"


def test_batch_allows_empty_answer_list() -> None:
    batch = CandidateFieldAnswers.model_validate({"answers": []})

    assert batch.answers == []


def test_batch_rejects_malformed_target_refs() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CandidateFieldAnswers.model_validate(
            {
                "answers": [
                    {
                        "source": "resume",
                        "field_label": "First Name",
                        "target": "not-a-ref!",
                        "value": "Chandrakanth",
                    }
                ]
            }
        )

    assert any(error["loc"] == ("answers", 0, "target") for error in exc_info.value.errors())


def test_batch_rejects_invalid_answer_source() -> None:
    with pytest.raises(ValidationError):
        CandidateFieldAnswers.model_validate(
            {
                "answers": [
                    {
                        "source": "invented",
                        "field_label": "First Name",
                        "target": "e70",
                        "value": "Chandrakanth",
                    }
                ]
            }
        )


def test_single_answer_is_still_valid_per_field_schema() -> None:
    answer = CandidateFieldAnswer(
        source="memory",
        field_label="Email",
        target="e90",
        value="candidate@example.com",
    )

    assert answer.field_label == "Email"
    assert answer.value == "candidate@example.com"
