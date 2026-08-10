from __future__ import annotations


class SubmissionGuard:
    """One-use final-submission approval gate.

    Human approval arms the gate; the first click that classifies as a real
    form submit consumes it. No submit control can be clicked before the
    human approves and no submit control can be clicked twice after it. The
    gate does not bind a DOM ref: the runtime re-resolves the submit control
    from live DOM at click time, so an SPA re-render can never stale the
    approval.
    """

    def __init__(self) -> None:
        self.active = False
        self._approved = False
        self._consumed = False

    def activate(self) -> None:
        self.active = True
        self._clear()

    def approve(self, approved: bool) -> None:
        self._approved = bool(approved)
        if approved:
            # A FRESH human approval re-arms the gate even after a previous
            # submit click was consumed but the submission did not verifiably
            # succeed (blocker/error). approve(False) (revoke) keeps the
            # consumed state so the gate stays locked.
            self._consumed = False

    def require_armed(self) -> None:
        if not self._approved or self._consumed:
            raise ValueError(
                "Final-form submission is locked: the human has not approved this "
                "application yet, or the approved submit click already happened. "
                "Request submission approval and wait for it before clicking the "
                "submit control."
            )

    def consume(self) -> None:
        self._consumed = True

    def is_consumed(self) -> bool:
        """True when an approved submit click already fired for this approval."""
        return self._consumed

    def _clear(self) -> None:
        self._approved = False
        self._consumed = False
