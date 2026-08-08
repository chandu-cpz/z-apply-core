from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from langchain_core.messages import ToolMessage

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
async def test_active_goal_emits_prose_stop_rejection_event() -> None:
    events: list[object] = []

    class Sink:
        def accept(self, event: object) -> None:
            events.append(event)

    middleware = ActiveGoalMiddleware(
        is_terminal=lambda: False,
        on_no_progress=lambda error: None,
        max_recoveries=3,
        sink=Sink(),  # type: ignore[arg-type]
    )

    await middleware.aafter_agent({}, None)

    assert len(events) == 1
    assert events[0].event == "active_objective_rejected"  # type: ignore[attr-defined]
    assert events[0].data["recovery"] == 1  # type: ignore[attr-defined]
    assert events[0].data["max"] == 3  # type: ignore[attr-defined]


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
async def test_read_only_tool_does_not_reset_consecutive_recovery_budget() -> None:
    middleware = ActiveGoalMiddleware(
        is_terminal=lambda: False,
        on_no_progress=lambda error: None,
        max_recoveries=1,
    )

    await middleware.aafter_agent({}, None)
    request = Mock(tool_call={"name": "browser_snapshot"})

    async def snapshot_handler(request: Any) -> ToolMessage:
        del request
        return ToolMessage(content="snapshot", tool_call_id="tool-1")

    await middleware.awrap_tool_call(request, snapshot_handler)

    with pytest.raises(ActiveGoalExhausted):
        await middleware.aafter_agent({}, None)


@pytest.mark.asyncio
async def test_successful_browser_mutation_resets_recovery_budget() -> None:
    middleware = ActiveGoalMiddleware(
        is_terminal=lambda: False,
        on_no_progress=lambda error: None,
        max_recoveries=1,
    )

    await middleware.aafter_agent({}, None)
    request = Mock(tool_call={"name": "browser_fill_form"})

    async def fill_handler(request: Any) -> ToolMessage:
        del request
        return ToolMessage(content="filled", tool_call_id="tool-2")

    await middleware.awrap_tool_call(request, fill_handler)

    update = await middleware.aafter_agent({}, None)

    assert update is not None
    assert update["jump_to"] == "model"


@pytest.mark.asyncio
async def test_failed_browser_mutation_does_not_reset_recovery_budget() -> None:
    middleware = ActiveGoalMiddleware(
        is_terminal=lambda: False,
        on_no_progress=lambda error: None,
        max_recoveries=1,
    )

    await middleware.aafter_agent({}, None)
    request = Mock(tool_call={"name": "browser_fill_form"})

    async def denied_fill_handler(request: Any) -> ToolMessage:
        del request
        return ToolMessage(
            content="denied",
            tool_call_id="tool-3",
            status="error",
        )

    await middleware.awrap_tool_call(request, denied_fill_handler)

    with pytest.raises(ActiveGoalExhausted):
        await middleware.aafter_agent({}, None)


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


@pytest.mark.asyncio
async def test_persistent_goal_stops_cleanly_when_recovery_decides_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from z_apply_core.agents.no_progress_guard import NoProgressCircuitOpen

    calls: list[tuple[Exception, int]] = []
    stalled = {"set": False}

    async def always_no_progress(**kwargs: object) -> None:
        del kwargs
        raise NoProgressCircuitOpen("no progress")

    monkeypatch.setattr(
        "z_apply_core.agents.goal_runner.run_active_goal",
        always_no_progress,
    )

    def on_recovery(exc: Exception, attempt: int) -> bool:
        calls.append((exc, attempt))
        if attempt >= 2:
            stalled["set"] = True
            return True
        return False

    await run_persistent_goal(
        object(),
        initial_message="objective",
        config={},
        sink=None,
        is_terminal=lambda: False,
        recovery_delay_seconds=0,
        on_recovery=on_recovery,
    )

    assert stalled["set"] is True
    assert [attempt for _, attempt in calls] == [1, 2]


@pytest.mark.asyncio
async def test_persistent_goal_ignores_recovery_decision_failures(
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
        raise TimeoutError("provider timeout")

    monkeypatch.setattr(
        "z_apply_core.agents.goal_runner.run_active_goal",
        fake_run_active_goal,
    )

    def broken_recovery(exc: Exception, attempt: int) -> bool:
        del exc, attempt
        raise RuntimeError("decision failed")

    with pytest.raises(TimeoutError):
        await run_persistent_goal(
            object(),
            initial_message="objective",
            config={},
            sink=None,
            is_terminal=lambda: False,
            recovery_delay_seconds=0,
            max_recoveries=2,
            on_recovery=broken_recovery,
        )

    assert len(messages) == 3
