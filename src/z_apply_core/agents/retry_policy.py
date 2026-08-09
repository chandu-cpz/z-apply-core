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


def is_network_error(exc: Exception) -> bool:
    """True for connection-level failures where the run should WAIT, not spray.

    A dead network (gateway unreachable, DNS failure, TLS/read timeout, remote
    end closed) cannot be fixed by an instant re-send. These need a long,
    paced backoff so the run pauses until connectivity returns. Queue-full and
    rate-limit responses are NOT network errors: they clear on their own and
    keep the provider's fast retry behavior.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    name = type(exc).__name__
    if name in {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "RemoteProtocolError",
        "NetworkError",
        "ProtocolError",
    }:
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "connection refused",
            "connection reset",
            "connection aborted",
            "connection closed",
            "failed to connect",
            "network is unreachable",
            "name or service not known",
            "temporarily unavailable",
            "remote end closed connection",
            "read timed out",
            "connect timed out",
            "timeout",
        )
    )


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


class NetworkLossRetryMiddleware(ModelRetryMiddleware):
    """Retry genuine network failures with a long, paced backoff.

    Named distinctly so it can stack with the provider's own
    ``ModelRetryMiddleware``: deepagents requires every middleware in a chain to
    have a unique name, and a dead network needs waiting, not instant re-spray.
    """

    def __init__(self) -> None:
        super().__init__(
            max_retries=40,
            retry_on=is_network_error,
            on_failure="error",
            initial_delay=5.0,
            backoff_factor=1.8,
            max_delay=60.0,
            jitter=True,
        )


def model_retry_middleware(provider: object | None = None) -> list[ModelRetryMiddleware]:
    """Retry transient model failures; PAUSE (long backoff) on network loss.

    Two stacked layers: the inner layer keeps the provider's fast retry pacing
    (instant for the queue-based opencode Zen gateway, paced backoff for other
    providers); the outer layer catches genuine network failures and waits with
    a long exponential backoff (5s up to 60s, up to 40 attempts) so a dead
    network pauses the run instead of failing fast or churning recovery. When
    the network is fine, the outer layer passes every error straight through to
    the inner layer's normal pacing.
    """
    if is_instant_retry_provider(provider):
        inner = ModelRetryMiddleware(
            max_retries=8,
            retry_on=should_retry_model_error,
            on_failure="error",
            initial_delay=0.0,
            backoff_factor=0.0,
            max_delay=0.0,
            jitter=False,
        )
    else:
        inner = ModelRetryMiddleware(
            max_retries=8,
            retry_on=should_retry_model_error,
            on_failure="error",
            initial_delay=1.0,
            backoff_factor=1.7,
            max_delay=12.0,
            jitter=True,
        )
    return [NetworkLossRetryMiddleware(), inner]
