from __future__ import annotations

from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from z_apply_core.agents.goal_runner import (
    ACTIVE_OBJECTIVE_SOURCE,
    ActiveGoalExhausted,
    ActiveGoalMiddleware,
    run_persistent_goal,
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


@pytest.mark.asyncio
async def test_native_tool_action_resets_consecutive_recovery_budget() -> None:
    middleware = ActiveGoalMiddleware(
        is_terminal=lambda: False,
        on_no_progress=lambda error: None,
        max_recoveries=1,
    )

    await middleware.aafter_agent({}, None)
    await middleware.aafter_model(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "browser_snapshot",
                            "args": {},
                            "id": "tool-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        None,
    )

    update = await middleware.aafter_agent({}, None)

    assert update is not None
    assert update["jump_to"] == "model"


@pytest.mark.asyncio
async def test_persistent_goal_reenters_same_agent_after_stream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []

    async def fake_run_active_goal(
        agent: object,
        *,
        initial_message: str,
        config: object,
        sink: object,
        source: str = "orchestrator",
    ) -> None:
        del agent, config, sink, source
        messages.append(initial_message)
        if len(messages) < 3:
            raise TimeoutError("provider timeout")

    monkeypatch.setattr(
        "z_apply_core.agents.goal_runner.run_active_goal",
        fake_run_active_goal,
    )

    await run_persistent_goal(
        object(),
        initial_message="original objective",
        config={},
        sink=None,
        is_terminal=lambda: False,
        recovery_delay_seconds=0,
    )

    assert len(messages) == 3
    assert messages[0] == "original objective"
    assert "same checkpointed thread" in messages[1]
    assert "Recovery attempt: 2/100" in messages[2]
