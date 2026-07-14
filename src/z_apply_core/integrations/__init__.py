"""The supported, transport-neutral integration surface for Z-Apply Core."""

from z_apply_core.integrations.events import CoreEventSink
from z_apply_core.integrations.exceptions import (
    BrowserControlConflict,
    BrowserUnavailable,
    CoreIntegrationError,
    CoreShuttingDown,
    HumanRequestAlreadyResolved,
    HumanRequestTypeMismatch,
    InvalidRunTransition,
    RunNotFound,
    SubmissionApprovalViolation,
)
from z_apply_core.integrations.models import (
    BrowserControlMode,
    BrowserTabState,
    CoreArtifact,
    CoreEvent,
    CoreHumanRequest,
    CoreIntegrationConfig,
    CoreLiveView,
    CoreRunResult,
    CoreRunView,
    RunOutcome,
    RunPhase,
    RunStatus,
    StartRunRequest,
)
from z_apply_core.integrations.service import CoreRunHandle, ZApplyCore

__all__ = [
    "BrowserControlConflict",
    "BrowserControlMode",
    "BrowserTabState",
    "BrowserUnavailable",
    "CoreArtifact",
    "CoreEvent",
    "CoreEventSink",
    "CoreHumanRequest",
    "CoreIntegrationConfig",
    "CoreIntegrationError",
    "CoreLiveView",
    "CoreRunHandle",
    "CoreRunResult",
    "CoreRunView",
    "CoreShuttingDown",
    "HumanRequestAlreadyResolved",
    "HumanRequestTypeMismatch",
    "InvalidRunTransition",
    "RunNotFound",
    "RunOutcome",
    "RunPhase",
    "RunStatus",
    "StartRunRequest",
    "SubmissionApprovalViolation",
    "ZApplyCore",
]
