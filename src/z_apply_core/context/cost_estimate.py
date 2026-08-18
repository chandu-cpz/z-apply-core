from __future__ import annotations

import os
from dataclasses import dataclass

# Estimated USD per 1M tokens. Defaults are current official public rates;
# every rate is overridable per provider with <PROVIDER>_PRICE_IN /
# <PROVIDER>_PRICE_OUT / <PROVIDER>_PRICE_CACHE_IN env vars (for example
# OPENCODEGO_PRICE_IN=0.5, GROQ_PRICE_OUT=0.4). Cache-hit input rates apply
# to tokens the provider reports as served from its prefix cache; providers
# without a cache rate bill all input at the miss rate.
_DEFAULT_RATES: dict[str, tuple[float, float, float | None]] = {
    # (input miss $/1M, output $/1M, cached-input $/1M)
    "opencodego": (0.14, 0.28, 0.0028),  # official DeepSeek V4 Flash rates
    "groq": (0.15, 0.40, None),
    "agnes": (0.15, 0.40, None),
    "inferx": (0.14, 0.28, 0.0028),  # deepseek-v4-flash-0731
    "opengateway": (0.15, 0.40, None),
}


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """One cost estimate for a completed model call."""

    usd: float
    cache_read_tokens: int = 0


def _env_rate(provider: str, kind: str) -> float | None:
    raw = os.environ.get(f"{provider}_PRICE_{kind}")
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def estimate_cost(
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> CostEstimate:
    """Estimate the USD cost of one call from per-1M-token rates.

    Rates come from env overrides first, then the default table. Input tokens
    the provider reports as cache hits are billed at the cache-hit rate when
    the provider defines one; the remainder bills at the miss rate.
    """
    upper = provider.upper()
    rate_in = _env_rate(upper, "IN")
    rate_out = _env_rate(upper, "OUT")
    rate_cache = _env_rate(upper, "CACHE_IN")
    default_in, default_out, default_cache = _DEFAULT_RATES.get(provider, (0.15, 0.40, None))
    rate_in = rate_in if rate_in is not None else default_in
    rate_out = rate_out if rate_out is not None else default_out
    rate_cache = rate_cache if rate_cache is not None else default_cache

    cache_read = max(0, min(cache_read_tokens, input_tokens))
    billed_input = max(0, input_tokens - cache_read)
    if rate_cache is not None:
        input_usd = billed_input * rate_in + cache_read * rate_cache
    else:
        input_usd = (billed_input + cache_read) * rate_in
    usd = (input_usd + output_tokens * rate_out) / 1_000_000
    return CostEstimate(usd=round(usd, 6), cache_read_tokens=cache_read)


def format_cost(estimate: CostEstimate) -> str:
    """Render one cost as a compact dollar string."""
    if estimate.usd < 0.0001:
        return "<$0.0001"
    return f"${estimate.usd:.4f}"
