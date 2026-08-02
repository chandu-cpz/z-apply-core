from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from z_apply_core.agents.no_progress_guard import (
    NoProgressCircuitOpen,
    NoProgressGuardMiddleware,
)


@pytest.mark.asyncio
async def test_repeated_denials_end_turn_for_persistent_recovery() -> None:
    failures: list[Exception] = []
    middleware = NoProgressGuardMiddleware(on_no_progress=failures.append)
    request = SimpleNamespace(tool_call={"name": "ls", "args": {"path": "/"}, "id": "call-1"})

    async def denied(_request: object) -> ToolMessage:
        return ToolMessage(
            content="Error: permission denied for read on /",
            name="ls",
            tool_call_id="call-1",
            status="error",
        )

    first = await middleware.awrap_tool_call(request, denied)  # type: ignore[arg-type]
    with pytest.raises(NoProgressCircuitOpen):
        await middleware.awrap_tool_call(request, denied)  # type: ignore[arg-type]

    assert first.content == "Error: permission denied for read on /"
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_successful_tool_churn_ends_turn_when_browser_does_not_advance() -> None:
    failures: list[Exception] = []
    browser = SimpleNamespace(current_observation=SimpleNamespace(signature="page-a"))
    middleware = NoProgressGuardMiddleware(
        browser=browser,
        max_stagnant_tool_calls=3,
        on_no_progress=failures.append,
    )

    async def success(request: object) -> ToolMessage:
        call = request.tool_call  # type: ignore[attr-defined]
        return ToolMessage(
            content="updated internal plan",
            name=str(call["name"]),
            tool_call_id=str(call["id"]),
        )

    for index in range(2):
        request = SimpleNamespace(
            tool_call={"name": "write_todos", "args": {"step": index}, "id": str(index)}
        )
        await middleware.awrap_tool_call(request, success)  # type: ignore[arg-type]

    request = SimpleNamespace(
        tool_call={"name": "write_todos", "args": {"step": 3}, "id": "3"}
    )
    with pytest.raises(NoProgressCircuitOpen):
        await middleware.awrap_tool_call(request, success)  # type: ignore[arg-type]

    assert len(failures) == 1


@pytest.mark.asyncio
async def test_browser_revision_change_resets_stagnant_tool_counter() -> None:
    browser = SimpleNamespace(current_observation=SimpleNamespace(signature="page-a"))
    middleware = NoProgressGuardMiddleware(browser=browser, max_stagnant_tool_calls=2)

    async def success(request: object) -> ToolMessage:
        call = request.tool_call  # type: ignore[attr-defined]
        if call["name"] == "browser_click":
            browser.current_observation = SimpleNamespace(signature="page-b")
        return ToolMessage(
            content="success",
            name=str(call["name"]),
            tool_call_id=str(call["id"]),
        )

    read = SimpleNamespace(tool_call={"name": "browser_find", "args": {}, "id": "1"})
    click = SimpleNamespace(
        tool_call={"name": "browser_click", "args": {"target": "e1"}, "id": "2"}
    )
    another_read = SimpleNamespace(
        tool_call={"name": "browser_snapshot", "args": {}, "id": "3"}
    )

    await middleware.awrap_tool_call(read, success)  # type: ignore[arg-type]
    await middleware.awrap_tool_call(click, success)  # type: ignore[arg-type]
    result = await middleware.awrap_tool_call(another_read, success)  # type: ignore[arg-type]

    assert result.status == "success"


@pytest.mark.asyncio
async def test_repeated_bookkeeping_model_choices_end_root_turn() -> None:
    failures: list[Exception] = []
    middleware = NoProgressGuardMiddleware(
        max_stagnant_model_responses=2,
        on_no_progress=failures.append,
    )
    request = SimpleNamespace()

    async def bookkeeping(_request: object) -> ModelResponse:
        return ModelResponse(
            result=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_todos",
                            "args": {"todos": []},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )

    await middleware.awrap_model_call(request, bookkeeping)  # type: ignore[arg-type]
    with pytest.raises(NoProgressCircuitOpen):
        await middleware.awrap_model_call(request, bookkeeping)  # type: ignore[arg-type]

    assert len(failures) == 1


@pytest.mark.asyncio
async def test_progress_model_choice_resets_bookkeeping_counter() -> None:
    middleware = NoProgressGuardMiddleware(max_stagnant_model_responses=2)
    request = SimpleNamespace()

    def response(tool_name: str) -> ModelResponse:
        return ModelResponse(
            result=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": tool_name,
                            "args": {},
                            "id": tool_name,
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )

    await middleware.awrap_model_call(  # type: ignore[arg-type]
        request, lambda _request: _async_result(response("write_todos"))
    )
    await middleware.awrap_model_call(  # type: ignore[arg-type]
        request, lambda _request: _async_result(response("browser_click"))
    )
    result = await middleware.awrap_model_call(  # type: ignore[arg-type]
        request, lambda _request: _async_result(response("write_todos"))
    )

    assert result.result[0].tool_calls[0]["name"] == "write_todos"  # type: ignore[union-attr]


async def _async_result(value: ModelResponse) -> ModelResponse:
    return value


async def _execute(
    middleware: NoProgressGuardMiddleware,
    tool_calls: list[dict],
) -> bool:
    """Run tool-call steps; return True if the no-progress circuit opened."""
    for index, call in enumerate(tool_calls):
        request = SimpleNamespace(
            tool_call={
                "name": call["name"],
                "args": call.get("args", {}),
                "id": call.get("id", f"call-{index}"),
            }
        )

        async def success(
            _request: object,
            *,
            _call: dict = call,
        ) -> ToolMessage:
            return ToolMessage(
                content=_call.get("result_content", "ok"),
                name=str(_call["name"]),
                tool_call_id=str(_call["id"]),
                status=_call.get("status", "success"),
            )

        try:
            await middleware.awrap_tool_call(request, success)  # type: ignore[arg-type]
        except NoProgressCircuitOpen:
            return True
    return False


@pytest.mark.asyncio
async def test_same_action_repeated_within_window_triggers() -> None:
    failures: list[Exception] = []
    middleware = NoProgressGuardMiddleware(
        window_size=8,
        repetition_threshold=4,
        on_no_progress=failures.append,
    )
    clicks = [
        {"name": "browser_click", "args": {"target": "e5"}, "id": f"c{i}"}
        for i in range(4)
    ]

    assert await _execute(middleware, clicks) is True
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_same_field_refilled_with_new_value_is_still_a_repeat() -> None:
    failures: list[Exception] = []
    middleware = NoProgressGuardMiddleware(
        window_size=8,
        repetition_threshold=4,
        on_no_progress=failures.append,
    )
    fills = [
        {
            "name": "browser_fill_form",
            "args": {"fields": [{"target": "e3", "value": f"value-{i}"}]},
            "id": f"f{i}",
        }
        for i in range(4)
    ]

    assert await _execute(middleware, fills) is True
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_same_tool_on_different_fields_does_not_trigger() -> None:
    middleware = NoProgressGuardMiddleware(window_size=8, repetition_threshold=4)
    fills = [
        {
            "name": "browser_fill_form",
            "args": {"fields": [{"target": f"e{i}", "value": "v"}]},
            "id": f"f{i}",
        }
        for i in range(4)
    ]

    assert await _execute(middleware, fills) is False


@pytest.mark.asyncio
async def test_repeat_spread_wider_than_window_does_not_trigger() -> None:
    middleware = NoProgressGuardMiddleware(window_size=4, repetition_threshold=3)
    steps = [
        {"name": "browser_click", "args": {"target": "e5"}, "id": "c1"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w1"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w2"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w3"},
        {"name": "browser_click", "args": {"target": "e5"}, "id": "c2"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w4"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w5"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w6"},
        {"name": "browser_click", "args": {"target": "e5"}, "id": "c3"},
    ]

    assert await _execute(middleware, steps) is False


@pytest.mark.asyncio
async def test_wider_window_catches_slow_loop() -> None:
    failures: list[Exception] = []
    slow_steps = [
        {"name": "browser_click", "args": {"target": "e5"}, "id": "c1"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w1"},
        {"name": "browser_click", "args": {"target": "e5"}, "id": "c2"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w2"},
        {"name": "browser_click", "args": {"target": "e5"}, "id": "c3"},
        {"name": "browser_wait_for", "args": {"time": 1}, "id": "w3"},
        {"name": "browser_click", "args": {"target": "e5"}, "id": "c4"},
    ]
    narrow = NoProgressGuardMiddleware(
        window_size=3,
        repetition_threshold=4,
        on_no_progress=failures.append,
    )
    assert await _execute(narrow, slow_steps) is False

    wide = NoProgressGuardMiddleware(
        window_size=8,
        repetition_threshold=4,
        on_no_progress=failures.append,
    )
    assert await _execute(wide, slow_steps) is True
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_detection_ignores_tool_result_text() -> None:
    middleware = NoProgressGuardMiddleware(window_size=8, repetition_threshold=4)
    varied_text = [
        {
            "name": "browser_click",
            "args": {"target": "e5"},
            "id": f"c{i}",
            "result_content": f"receipt number {i}",
        }
        for i in range(4)
    ]
    assert await _execute(middleware, varied_text) is True

    middleware = NoProgressGuardMiddleware(window_size=8, repetition_threshold=4)
    same_text_different_refs = [
        {
            "name": "browser_click",
            "args": {"target": f"e{i}"},
            "id": f"c{i}",
            "result_content": "identical receipt text",
        }
        for i in range(4)
    ]
    assert await _execute(middleware, same_text_different_refs) is False


@pytest.mark.asyncio
async def test_model_call_detection_ignores_assistant_text() -> None:
    middleware = NoProgressGuardMiddleware(max_stagnant_model_responses=2)
    request = SimpleNamespace()

    def response(tool_name: str, content: str) -> ModelResponse:
        return ModelResponse(
            result=[
                AIMessage(
                    content=content,
                    tool_calls=[
                        {
                            "name": tool_name,
                            "args": {},
                            "id": tool_name,
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )

    with pytest.raises(NoProgressCircuitOpen):
        await middleware.awrap_model_call(  # type: ignore[arg-type]
            request, lambda _request: _async_result(response("write_todos", "plan A"))
        )
        await middleware.awrap_model_call(  # type: ignore[arg-type]
            request, lambda _request: _async_result(response("write_todos", "plan B"))
        )


@pytest.mark.asyncio
async def test_model_call_detection_depends_on_tool_choice_not_text() -> None:
    middleware = NoProgressGuardMiddleware(max_stagnant_model_responses=2)
    request = SimpleNamespace()

    def response(tool_name: str) -> ModelResponse:
        return ModelResponse(
            result=[
                AIMessage(
                    content="identical assistant wording",
                    tool_calls=[
                        {
                            "name": tool_name,
                            "args": {},
                            "id": tool_name,
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )

    await middleware.awrap_model_call(  # type: ignore[arg-type]
        request, lambda _request: _async_result(response("write_todos"))
    )
    await middleware.awrap_model_call(  # type: ignore[arg-type]
        request, lambda _request: _async_result(response("browser_click"))
    )
    result = await middleware.awrap_model_call(  # type: ignore[arg-type]
        request, lambda _request: _async_result(response("write_todos"))
    )

    assert result.result[0].tool_calls[0]["name"] == "write_todos"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_identical_successful_read_is_not_executed_twice() -> None:
    middleware = NoProgressGuardMiddleware()
    request = SimpleNamespace(
        tool_call={
            "name": "browser_snapshot",
            "args": {},
            "id": "call-1",
        }
    )
    executions = 0

    async def lookup(_request: object) -> ToolMessage:
        nonlocal executions
        executions += 1
        return ToolMessage(
            content="current browser evidence",
            name="browser_snapshot",
            tool_call_id="call-1",
        )

    first = await middleware.awrap_tool_call(request, lookup)  # type: ignore[arg-type]
    second = await middleware.awrap_tool_call(request, lookup)  # type: ignore[arg-type]

    assert first.status == "success"
    assert second.status == "error"
    assert "already succeeded" in str(second.content)
    assert executions == 1


@pytest.mark.asyncio
async def test_successful_mutation_allows_same_read_again() -> None:
    middleware = NoProgressGuardMiddleware()
    read = SimpleNamespace(
        tool_call={"name": "browser_snapshot", "args": {}, "id": "read-1"}
    )
    mutation = SimpleNamespace(
        tool_call={"name": "browser_fill_form", "args": {}, "id": "write-1"}
    )
    executions = 0

    async def success(request: object) -> ToolMessage:
        nonlocal executions
        executions += 1
        call = request.tool_call  # type: ignore[attr-defined]
        return ToolMessage(
            content="success",
            name=str(call["name"]),
            tool_call_id=str(call["id"]),
        )

    await middleware.awrap_tool_call(read, success)  # type: ignore[arg-type]
    await middleware.awrap_tool_call(mutation, success)  # type: ignore[arg-type]
    repeated = await middleware.awrap_tool_call(read, success)  # type: ignore[arg-type]

    assert repeated.status == "success"
    assert executions == 3


@pytest.mark.asyncio
async def test_failed_action_is_blocked_until_browser_evidence_changes() -> None:
    observation = SimpleNamespace(signature="page-a")
    browser = SimpleNamespace(current_observation=observation)
    middleware = NoProgressGuardMiddleware(
        browser=browser,
        max_identical_denials=99,
        max_non_progress=99,
        max_state_action_failures=3,
    )
    request = SimpleNamespace(
        tool_call={"name": "browser_click", "args": {"target": "e7"}, "id": "call"}
    )
    executions = 0

    async def failed(_request: object) -> ToolMessage:
        nonlocal executions
        executions += 1
        return ToolMessage(
            content="target unavailable",
            name="browser_click",
            tool_call_id="call",
            status="error",
        )

    for _ in range(3):
        await middleware.awrap_tool_call(request, failed)  # type: ignore[arg-type]
    blocked = await middleware.awrap_tool_call(request, failed)  # type: ignore[arg-type]

    assert executions == 3
    assert "STATE-ACTION CIRCUIT" in str(blocked.content)

    browser.current_observation = SimpleNamespace(signature="page-b")
    retried = await middleware.awrap_tool_call(request, failed)  # type: ignore[arg-type]

    assert executions == 4
    assert "target unavailable" in str(retried.content)
