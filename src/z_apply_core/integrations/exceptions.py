"""Stable exceptions exposed by the Core integration boundary."""

from __future__ import annotations


class CoreIntegrationError(Exception):
    """Base class for errors callers may map without parsing messages."""


class RunNotFound(CoreIntegrationError):
    pass


class InvalidRunTransition(CoreIntegrationError):
    pass


class HumanRequestAlreadyResolved(CoreIntegrationError):
    pass


class HumanRequestTypeMismatch(CoreIntegrationError):
    pass


class BrowserUnavailable(CoreIntegrationError):
    pass


class BrowserControlConflict(CoreIntegrationError):
    pass


class SubmissionApprovalViolation(CoreIntegrationError):
    pass


class CoreShuttingDown(CoreIntegrationError):
    pass
