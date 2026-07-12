from __future__ import annotations

from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen
from z_apply_core.agents.protocol_guard import ToolProtocolViolation
from z_apply_core.agents.terminal_guard import TerminalDecisionRecorded


def should_retry_model_error(exc: Exception) -> bool:
    """Return True for transport/provider failures safe to retry.

    Returns False for intentional agent or runtime control failures that
    carry their own recovery semantics.
    """
    blocked = (ToolProtocolViolation, NoProgressCircuitOpen, TerminalDecisionRecorded)
    return not isinstance(exc, blocked)
