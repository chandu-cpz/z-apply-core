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


@pytest.mark.asyncio
async def test_lease_uses_deepseek_native_reasoning_client() -> None:
    from langchain_deepseek import ChatDeepSeek

    llm = await _lease_llm()

    assert isinstance(llm, ChatDeepSeek)
    assert llm.api_base == "https://apihub.agnes-ai.com/v1"


def test_reasoning_surfaces_out_of_content_for_stream_projection() -> None:
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(
        content="answer",
        additional_kwargs={"reasoning_content": "think step"},
    )
    assert chunk.content == "answer"
    assert chunk.content_blocks == [
        {"type": "reasoning", "reasoning": "think step"},
        {"type": "text", "text": "answer"},
    ]


def test_reasoned_assistant_replays_without_reasoning_in_content() -> None:
    """Regression guard for the Agnes 400 loop.

    Agnes rejects assistant messages whose content carries reasoning parts.
    langchain-deepseek keeps reasoning in ``additional_kwargs`` and outbound
    serialization drops it from content, so replayed history stays within the
    OpenAI content-part enum even when the turn made tool calls.
    """
    from langchain_core.messages import AIMessage
    from langchain_openai.chat_models.base import _convert_message_to_dict

    message = AIMessage(
        content="Proceeding.",
        additional_kwargs={"reasoning_content": "think step"},
        tool_calls=[
            {
                "name": "browser_click",
                "args": {"target": "e140"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )

    body = _convert_message_to_dict(message)

    assert body["role"] == "assistant"
    assert body["content"] == "Proceeding."
    assert body["tool_calls"][0]["function"]["name"] == "browser_click"
