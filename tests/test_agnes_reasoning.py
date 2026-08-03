from __future__ import annotations

import pytest

from z_apply_core.agents.model_provider import AgnesProvider
from z_apply_core.config import load_settings


async def _lease_llm(**kwargs: object) -> object:
    provider = AgnesProvider(api_key="test-key", **kwargs)
    selection = await provider.lease()
    return selection.llm


def test_default_reasoning_is_enabled() -> None:
    assert AgnesProvider(api_key="test-key")._reasoning is True


@pytest.mark.asyncio
async def test_default_reasoning_uses_documented_chat_template_kwargs() -> None:
    llm = await _lease_llm()

    assert llm.extra_body == {
        "chat_template_kwargs": {"enable_thinking": True},
    }


@pytest.mark.asyncio
async def test_reasoning_off_disables_thinking() -> None:
    llm = await _lease_llm(reasoning=False)

    assert llm.extra_body == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_settings_defaults_apply_when_env_unset() -> None:
    load_settings.cache_clear()
    settings = load_settings()

    assert settings.agnes_reasoning is True
