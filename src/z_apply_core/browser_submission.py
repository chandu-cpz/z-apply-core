from __future__ import annotations

from z_apply_core.browser_observation import BrowserObservation


class SubmissionGuard:
    """One-use final-submission approval state."""

    def __init__(self) -> None:
        self.active = False
        self._target = ""
        self._revision = 0
        self._signature = ""
        self._approved = False
        self._consumed = False

    def activate(self) -> None:
        self.active = True
        self._clear()

    def approve(self, approved: bool) -> None:
        if not approved:
            self._clear()
        elif not self._target:
            raise ValueError("Submission approval has no pending reviewed browser target.")
        else:
            self._approved = True

    def prepare(
        self,
        *,
        target: str,
        observation: BrowserObservation,
    ) -> None:
        self._target = target
        self._revision = observation.revision
        self._signature = observation.signature
        self._approved = False
        self._consumed = False

    def require_target(self, target: object) -> None:
        if not self._target or not self._approved or self._consumed or target != self._target:
            raise ValueError(
                "Final-form submission is locked. Approval must match the exact current "
                "submit control reviewed through request_submit_approval."
            )

    def require_observation(self, observation: BrowserObservation) -> None:
        if observation.revision != self._revision or observation.signature != self._signature:
            self._clear()
            raise ValueError(
                "Submission approval was revoked because the reviewed browser state changed. "
                "Inspect, review, and request approval again."
            )

    def consume(self) -> None:
        self._consumed = True

    def _clear(self) -> None:
        self._target = ""
        self._revision = 0
        self._signature = ""
        self._approved = False
        self._consumed = False
