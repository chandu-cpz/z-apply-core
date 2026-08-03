from __future__ import annotations

import pytest

from z_apply_core.agents.model_provider import InferXProvider
from z_apply_core.config import load_settings


async def _lease_llm(**kwargs: object) -> object:
    provider = InferXProvider(api_key="test-key", **kwargs)
    selection = await provider.lease()
    return selection.llm


@pytest.mark.asyncio
async def test_default_reasoning_sends_enabled_thinking_and_high_effort() -> None:
    llm = await _lease_llm()

    assert llm.extra_body == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
async def test_reasoning_off_sends_disabled_thinking() -> None:
    llm = await _lease_llm(reasoning=False)

    assert llm.extra_body == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_reasoning_effort_passthrough() -> None:
    llm = await _lease_llm(reasoning_effort="max")

    assert llm.extra_body["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_invalid_reasoning_effort_falls_back_to_high() -> None:
    llm = await _lease_llm(reasoning_effort="bogus")

    assert llm.extra_body["reasoning_effort"] == "high"


def test_settings_defaults_apply_when_env_unset() -> None:
    load_settings.cache_clear()
    settings = load_settings()

    assert settings.inferx_reasoning is True
    assert settings.inferx_reasoning_effort == "high"
