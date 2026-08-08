from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telegram.error import BadRequest, NetworkError, TimedOut

from z_apply_core.human.telegram import _retry_network_call


@pytest.mark.asyncio
class TestRetryNetworkCall:
    async def test_succeeds_first_attempt(self) -> None:
        factory = AsyncMock(return_value="ok")
        assert await _retry_network_call(factory, attempts=3) == "ok"
        factory.assert_awaited_once()

    async def test_retries_then_succeeds(self) -> None:
        calls = 0

        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TimedOut()
            return "ok"

        assert await _retry_network_call(flaky, attempts=3, backoff=0.01) == "ok"
        assert calls == 3

    async def test_retries_network_error(self) -> None:
        calls = 0

        async def flaky() -> str:
            nonlocal calls
            calls += 1
            raise NetworkError("connection reset")

        with pytest.raises(NetworkError):
            await _retry_network_call(flaky, attempts=3, backoff=0.01)
        assert calls == 3

    async def test_non_network_error_propagates_immediately(self) -> None:
        factory = AsyncMock(side_effect=BadRequest("chat not found"))
        with pytest.raises(BadRequest):
            await _retry_network_call(factory, attempts=3)
        factory.assert_awaited_once()
