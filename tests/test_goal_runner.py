from __future__ import annotations

from unittest.mock import Mock

import pytest

from z_apply_core.agents.goal_runner import (
    ACTIVE_OBJECTIVE_SOURCE,
    ActiveGoalExhausted,
    ActiveGoalMiddleware,
)
from z_apply_core.agents.protocol_guard import ToolProtocolViolation


@pytest.mark.asyncio
async def test_active_goal_rejects_prose_stop_and_jumps_to_model() -> None:
    reject = Mock()
    middleware = ActiveGoalMiddleware(
        is_terminal=lambda: False,
        on_no_progress=reject,
    )

    update = await middleware.aafter_agent({}, None)

    assert update is not None
    assert update["jump_to"] == "model"
    assert update["messages"][0].name == ACTIVE_OBJECTIVE_SOURCE
    assert isinstance(reject.call_args.args[0], ToolProtocolViolation)


@pytest.mark.asyncio
async def test_active_goal_allows_typed_terminal_stop() -> None:
    reject = Mock()
    middleware = ActiveGoalMiddleware(
        is_terminal=lambda: True,
        on_no_progress=reject,
    )

    assert await middleware.aafter_agent({}, None) is None
    reject.assert_not_called()


@pytest.mark.asyncio
async def test_active_goal_has_bounded_recovery() -> None:
    middleware = ActiveGoalMiddleware(
        is_terminal=lambda: False,
        on_no_progress=lambda error: None,
        max_recoveries=1,
    )

    await middleware.aafter_agent({}, None)
    with pytest.raises(ActiveGoalExhausted):
        await middleware.aafter_agent({}, None)
