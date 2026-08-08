from __future__ import annotations

from langchain.agents.middleware import ModelRetryMiddleware

from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen


def is_instant_retry_provider(provider: object) -> bool:
    """True when a provider wants immediate retries with no backoff sleep.

    The opencode Zen gateway is queue-based: a full queue clears on its own,
    and a fresh request made immediately usually succeeds. Sleeping only
    delays the retry without improving its odds, so OpenCodeGo retries
    instantly while every other provider keeps backoff pacing.
    """
    if provider is None:
        return False
    from z_apply_core.agents.model_provider import OpenCodeGoProvider

    return isinstance(provider, OpenCodeGoProvider)


def should_retry_model_error(exc: Exception) -> bool:
    """Return True for transport/provider failures safe to retry.

    Returns False for intentional agent or runtime control failures that
    carry their own recovery semantics, and for permanent client errors
    (HTTP 400 invalid_request_error and friends) that re-sending the same
    request can never fix.
    """
    if isinstance(exc, NoProgressCircuitOpen):
        return False
    status_code = getattr(exc, "status_code", None)
    return not (isinstance(status_code, int) and status_code in {400, 401, 403, 404, 422})


def model_retry_middleware(provider: object | None = None) -> ModelRetryMiddleware:
    """Retry transient model failures long enough for router cooldowns to rotate.

    When *provider* is the opencode Zen gateway (``OpenCodeGoProvider``),
    retries run immediately with zero backoff: the gateway queue clears on
    its own and an instant re-request succeeds, so any sleep is pure stall.
    """
    if is_instant_retry_provider(provider):
        return ModelRetryMiddleware(
            max_retries=8,
            retry_on=should_retry_model_error,
            on_failure="error",
            initial_delay=0.0,
            backoff_factor=0.0,
            max_delay=0.0,
            jitter=False,
        )
    return ModelRetryMiddleware(
        max_retries=8,
        retry_on=should_retry_model_error,
        on_failure="error",
        initial_delay=1.0,
        backoff_factor=1.7,
        max_delay=12.0,
        jitter=True,
    )
