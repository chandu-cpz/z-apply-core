from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telegram.error import TimedOut

from z_apply_core.human.channel import HumanChannel
from z_apply_core.human.tools import make_human_tools

_TIMED_OUT = TimedOut()


class _FailingChannel:
    """A HumanChannel whose ask always fails like a flaky Telegram network."""

    def __init__(self, error: Exception = _TIMED_OUT) -> None:
        self._error = error

    async def ask(self, **_: Any) -> str:
        raise self._error

    async def send_artifact(self, **_: Any) -> None:
        return None

    async def confirm(self, **_: Any) -> bool:
        return False


async def _call_ask_human(channel: HumanChannel) -> dict[str, str]:
    tools = make_human_tools(channel)
    ask_human = next(tool for tool in tools if tool.name == "ask_human")
    return await ask_human.ainvoke(
        {
            "question": "Are you open to relocating to Mumbai?",
            "reason": "missing_candidate_fact",
            "field_label": "Are you Open to Relocate to Mumbai?",
            "options": ["Yes", "No"],
        }
    )


@pytest.mark.asyncio
async def test_ask_human_returns_typed_error_on_channel_failure() -> None:
    result = await _call_ask_human(_FailingChannel())
    assert result["human_answer"] == ""
    assert "error" in result


@pytest.mark.asyncio
async def test_ask_human_never_raises_on_telegram_timeout() -> None:
    channel = _FailingChannel(error=TimedOut())
    try:
        await _call_ask_human(channel)
    except Exception as exc:  # pragma: no cover - failure is the assertion
        raise AssertionError(f"ask_human raised instead of returning a typed error: {exc}") from exc


@pytest.mark.asyncio
async def test_ask_human_propagates_real_answer() -> None:
    channel = AsyncMock(spec=HumanChannel)
    channel.ask.return_value = "Yes"
    result = await _call_ask_human(channel)
    assert result["human_answer"] == "Yes"
    channel.ask.assert_awaited_once()
