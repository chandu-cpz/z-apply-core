from __future__ import annotations

from z_apply_core.agents.router_middleware import ORCHESTRATOR_EXCLUDED_MODEL_IDS
from z_apply_core.model_policy import BLOCKED_MODEL_IDS_BELOW_120B


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
