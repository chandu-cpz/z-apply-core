from __future__ import annotations

from z_apply_core.agents.goal_runner import ActiveGoalExhausted
from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen
from z_apply_core.agents.orchestrator import decide_goal_stall


def test_non_no_progress_recovery_resets_the_counter() -> None:
    count, terminate = decide_goal_stall(TimeoutError("provider timeout"), 2)

    assert (count, terminate) == (0, False)


def test_no_progress_recoveries_accumulate_on_frozen_evidence() -> None:
    signature = "frozen-signature"
    count, terminate = decide_goal_stall(
        NoProgressCircuitOpen("no progress"),
        0,
        observation_signature=signature,
        previous_signature=signature,
    )
    assert (count, terminate) == (1, False)

    count, terminate = decide_goal_stall(
        NoProgressCircuitOpen("no progress"),
        count,
        observation_signature=signature,
        previous_signature=signature,
    )
    assert (count, terminate) == (2, True)


def test_advanced_evidence_between_recoveries_resets_the_counter() -> None:
    count, terminate = decide_goal_stall(
        ActiveGoalExhausted("exhausted"),
        2,
        observation_signature="page-a",
        previous_signature="page-a",
    )
    assert (count, terminate) == (3, True)

    # Real progress happened between recoveries: page evidence advanced.
    count, terminate = decide_goal_stall(
        ActiveGoalExhausted("exhausted"),
        count,
        observation_signature="page-b",
        previous_signature="page-a",
    )
    assert (count, terminate) == (1, False)


def test_stall_limit_is_respected() -> None:
    count, terminate = decide_goal_stall(
        NoProgressCircuitOpen("no progress"),
        0,
        limit=2,
        observation_signature="s",
        previous_signature="s",
    )
    assert (count, terminate) == (1, False)

    count, terminate = decide_goal_stall(
        NoProgressCircuitOpen("no progress"),
        count,
        limit=2,
        observation_signature="s",
        previous_signature="s",
    )
    assert (count, terminate) == (2, True)


def test_missing_evidence_does_not_reset_and_accumulates() -> None:
    count, terminate = decide_goal_stall(NoProgressCircuitOpen("no progress"), 2)

    assert (count, terminate) == (3, True)
