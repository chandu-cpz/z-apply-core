from __future__ import annotations

from z_apply_core.agents.router_middleware import ORCHESTRATOR_EXCLUDED_MODEL_IDS
from z_apply_core.model_policy import (
    BLOCKED_MODEL_IDS_BELOW_120B,
    VERIFIED_LARGE_TOOL_MODEL_IDS,
)


def test_router_policy_keeps_the_120b_floor_and_known_bad_120b_exceptions() -> None:
    blocked = set(BLOCKED_MODEL_IDS_BELOW_120B)

    assert "nvidia/nemotron-3-nano-30b-a3b" in blocked
    assert "qwen/qwen3-next-80b-a3b-instruct" in blocked
    assert "nvidia/nemotron-3-ultra-550b-a55b" not in blocked
    assert "nvidia/llama-3.1-nemotron-ultra-253b-v1" not in blocked
    assert "meta/llama-3.1-405b-instruct" not in blocked
    assert "moonshotai/kimi-k2-instruct" not in blocked
    assert "nvidia/nemotron-3-super-120b-a12b" in ORCHESTRATOR_EXCLUDED_MODEL_IDS
    assert "openai/gpt-oss-120b" in ORCHESTRATOR_EXCLUDED_MODEL_IDS


def test_job_agents_are_limited_to_locally_verified_large_tool_models() -> None:
    assert VERIFIED_LARGE_TOOL_MODEL_IDS == (
        "nvidia/nemotron-3-ultra-550b-a55b",
        "stepfun-ai/step-3.5-flash",
        "stepfun-ai/step-3.7-flash",
        "mistralai/mistral-medium-3.5-128b",
        "qwen/qwen3.5-122b-a10b",
    )
