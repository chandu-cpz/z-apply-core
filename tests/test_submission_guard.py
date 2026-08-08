from __future__ import annotations

import pytest

from z_apply_core.browser_submission import SubmissionGuard


def _approve(guard: SubmissionGuard) -> None:
    guard.activate()
    guard.approve(True)


def test_approval_allows_exactly_one_successful_submit() -> None:
    guard = SubmissionGuard()
    _approve(guard)
    guard.require_armed()
    guard.consume()

    with pytest.raises(ValueError, match="locked"):
        guard.require_armed()


def test_unapproved_submit_is_locked() -> None:
    guard = SubmissionGuard()
    guard.activate()

    with pytest.raises(ValueError, match="locked"):
        guard.require_armed()


def test_rejection_keeps_submit_locked() -> None:
    guard = SubmissionGuard()
    guard.activate()
    guard.approve(False)

    with pytest.raises(ValueError, match="locked"):
        guard.require_armed()


def test_revocation_keeps_guard_active() -> None:
    guard = SubmissionGuard()
    _approve(guard)
    guard._clear()

    with pytest.raises(ValueError, match="locked"):
        guard.require_armed()
