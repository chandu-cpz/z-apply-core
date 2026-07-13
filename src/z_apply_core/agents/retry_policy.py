from __future__ import annotations

from langchain.agents.middleware import ModelRetryMiddleware

from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen


def should_retry_model_error(exc: Exception) -> bool:
    """Return True for transport/provider failures safe to retry.

    Returns False for intentional agent or runtime control failures that
    carry their own recovery semantics.
    """
    blocked = (NoProgressCircuitOpen,)
    return not isinstance(exc, blocked)


def model_retry_middleware() -> ModelRetryMiddleware:
    """Retry transient model failures long enough for router cooldowns to rotate."""
    return ModelRetryMiddleware(
        max_retries=8,
        retry_on=should_retry_model_error,
        on_failure="error",
        initial_delay=1.0,
        backoff_factor=1.7,
        max_delay=12.0,
        jitter=True,
    )
