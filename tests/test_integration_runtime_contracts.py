from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from z_apply_core.agents.context_inbox import ContextInbox, ContextMessage
from z_apply_core.browser_pool import BrowserPool
from z_apply_core.browser_workspace import BrowserControlGate, BrowserWorkspace
from z_apply_core.human.broker import HumanRequestBroker
from z_apply_core.integrations.models import StartRunRequest
from z_apply_core.integrations.service import _framework_payload, _GraphSink, _Run
from z_apply_core.stream_events import FrameworkTraceEvent


def test_context_inbox_delivers_each_message_once_in_order() -> None:
    inbox = ContextInbox(max_pending=2)
    inbox.put(ContextMessage("first", "web"))
    inbox.put(ContextMessage("second", "web"))

    assert [message.content for message in inbox.drain()] == ["first", "second"]
    assert inbox.drain() == ()


@pytest.mark.asyncio
async def test_human_broker_is_first_response_wins() -> None:
    requested = []
    resolved = []
    broker = HumanRequestBroker(
        run_id="run-1",
        on_requested=lambda request: _append(requested, request),
        on_resolved=lambda request: _append(resolved, request),
    )

    waiting = asyncio.create_task(
        broker.request(
            kind="question",
            question="What is your notice period?",
            context="Required field",
            url="https://example.test/job",
            company="Example",
            role="Engineer",
            options=["Immediate", "30 days"],
            risk="medium",
            image_path="",
        )
    )
    await asyncio.sleep(0)
    request_id = requested[0].request_id
    assert str(UUID(request_id)) == request_id
    await broker.resolve_answer(request_id, "Immediate", responder="web")

    assert await waiting == "Immediate"
    assert resolved[0].responder == "web"
    with pytest.raises(KeyError):
        await broker.resolve_answer(request_id, "30 days", responder="telegram")


@pytest.mark.asyncio
async def test_browser_control_waits_for_mutation_and_blocks_new_mutations() -> None:
    gate = BrowserControlGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()

    async def mutation() -> None:
        async with gate.mutation():
            entered.set()
            await release.wait()

    first = asyncio.create_task(mutation())
    await entered.wait()
    takeover = asyncio.create_task(gate.take())
    await asyncio.sleep(0)

    async def second_mutation() -> None:
        async with gate.mutation():
            second_entered.set()

    second = asyncio.create_task(second_mutation())
    release.set()
    await first
    await takeover
    assert not second_entered.is_set()

    await gate.release()
    await second
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_browser_gate_serializes_operations_across_run_sessions() -> None:
    gate = BrowserControlGate()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_operation() -> None:
        async with gate.mutation():
            first_entered.set()
            await release_first.wait()

    async def second_operation() -> None:
        async with gate.mutation():
            second_entered.set()

    first = asyncio.create_task(first_operation())
    await first_entered.wait()
    second = asyncio.create_task(second_operation())
    await asyncio.sleep(0)

    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first, second)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_return_control_stops_stalled_load_and_retries_snapshot() -> None:
    workspace = BrowserWorkspace()
    calls = 0

    async def snapshot() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()
        return "fresh evidence"

    lease = SimpleNamespace(
        closed=False,
        focus=AsyncMock(),
        session=SimpleNamespace(capture_control_return_evidence=snapshot),
        stop_loading=AsyncMock(),
    )
    workspace._leases["run-1"] = lease
    await workspace.gate.take()

    with patch(
        "z_apply_core.browser_workspace._RETURN_CONTROL_SNAPSHOT_TIMEOUT_SECONDS",
        0.01,
    ):
        evidence = await workspace.return_agent_control("run-1")

    assert evidence == "fresh evidence"
    assert not workspace.gate.human_control
    lease.stop_loading.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_workspace_initializes_once_for_concurrent_runs() -> None:
    workspace = BrowserWorkspace()
    # Isolate from the real master/profile slots: the default pool provisions
    # real directories.
    workspace._pool = SimpleNamespace(provision=AsyncMock())  # type: ignore[method-assign]

    with (
        patch("z_apply_core.browser_workspace.VirtualDisplaySession.start") as display_start,
        patch("z_apply_core.browser_workspace.LiveView.start"),
    ):
        await asyncio.gather(workspace.start(), workspace.start(), workspace.start())

    display_start.assert_called_once()
    assert workspace._started


@pytest.mark.asyncio
async def test_browser_workspace_opens_per_run_instances_from_the_pool(tmp_path: Path) -> None:
    # Each run gets its OWN backend/context/tab from the pool (dedicated
    # instance), not a tab in one shared context.
    tabs = [SimpleNamespace(page=object()), SimpleNamespace(page=object())]
    contexts = [
        SimpleNamespace(new_tab=AsyncMock(side_effect=[tabs[0]]), tabs=lambda: [tabs[0]]),
        SimpleNamespace(new_tab=AsyncMock(side_effect=[tabs[1]]), tabs=lambda: [tabs[1]]),
    ]
    backends = [object(), object()]
    servers = [
        SimpleNamespace(backend_pool=SimpleNamespace(tools=(), backend_for=AsyncMock(return_value=backends[0]))),
        SimpleNamespace(backend_pool=SimpleNamespace(tools=(), backend_for=AsyncMock(return_value=backends[1]))),
    ]
    launcher_calls = []

    async def fake_launcher(run_id: str, profile_dir: Path) -> tuple[Any, Any, Any]:
        launcher_calls.append((run_id, profile_dir))
        idx = len(launcher_calls) - 1
        return servers[idx], backends[idx], contexts[idx]

    master = tmp_path / "master"
    (master / "storage" / "default" / "moz-extension+++addon-1").mkdir(parents=True)
    (master / "extensions.json").write_text("{}")
    (master / "prefs.js").write_text("user_pref('x', true);")
    (master / "cookies.sqlite").write_bytes(b"cookies")

    pool = BrowserPool(
        master=master,
        slots_root=tmp_path / "slots",
        slot_count=2,
        max_active=2,
        launcher=fake_launcher,
        launch_timeout=5,
        probe_timeout=2,
        close_timeout=2,
    )
    workspace = BrowserWorkspace(pool=pool)

    with (
        patch("z_apply_core.browser_workspace.VirtualDisplaySession.start"),
        patch("z_apply_core.browser_workspace.LiveView.start"),
    ):
        await workspace.start()
        first, second = await asyncio.gather(
            workspace.open_run("run-1"), workspace.open_run("run-2")
        )

    assert first.backend is backends[0] and second.backend is backends[1]
    assert first.context is contexts[0] and second.context is contexts[1]
    assert first.primary_tab is tabs[0] and second.primary_tab is tabs[1]
    assert [call[0] for call in launcher_calls] == ["run-1", "run-2"]


@pytest.mark.asyncio
async def test_browser_workspace_close_run_releases_the_pool_lease() -> None:
    workspace = BrowserWorkspace()
    lease = SimpleNamespace(
        closed=False,
        close_pages=AsyncMock(),
    )
    workspace._leases["run-1"] = lease
    release_calls: list[str] = []

    async def fake_release(run_id: str, *, reason: str) -> None:
        release_calls.append(run_id)

    workspace._pool.release = fake_release  # type: ignore[method-assign]

    with patch("z_apply_core.browser_workspace.VirtualDisplaySession.start"):
        await workspace.close_run("run-1")

    assert release_calls == ["run-1"]
    lease.close_pages.assert_awaited_once()


def test_framework_state_event_never_serializes_runtime_objects() -> None:
    runtime = object()
    payload = _framework_payload(
        FrameworkTraceEvent(
            "values",
            "values",
            {
                "data": {
                    "runtime": runtime,
                    "browser_tools": [runtime],
                    "snapshot": "large private browser state",
                    "run_status": "running",
                    "model_id": "provider/model",
                }
            },
            {},
        )
    )

    assert payload == {"run_status": "running", "model_id": "provider/model"}


@pytest.mark.asyncio
async def test_graph_sink_does_not_publish_token_fragments() -> None:
    class Service:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.live_events: list[str] = []

        async def _emit(
            self, run: object, event_type: str, *args: object, **kwargs: object
        ) -> None:
            self.events.append(event_type)

        async def _emit_live(
            self, run: object, event_type: str, *args: object, **kwargs: object
        ) -> None:
            self.live_events.append(event_type)

    service = Service()
    run = _Run(StartRunRequest(job_url="https://example.com/job"), "run-1")
    sink = _GraphSink(service, run)  # type: ignore[arg-type]

    await sink.accept(
        FrameworkTraceEvent(
            "agent_message_delta",
            "orchestrator:invocation-id",
            {"text": "partial reasoning"},
            {},
        )
    )
    await sink.accept(
        FrameworkTraceEvent(
            "agent_tool_start",
            "orchestrator:invocation-id",
            {"tool": "task"},
            {},
        )
    )

    assert service.events == ["tool.started"]
    assert service.live_events == ["agent.message.delta"]
    assert run.view.current_agent == "orchestrator"


@pytest.mark.asyncio
async def test_graph_sink_deduplicates_public_state_and_tracks_auth_model() -> None:
    class Service:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def _emit(
            self, run: object, event_type: str, *args: object, **kwargs: object
        ) -> None:
            self.events.append(event_type)

    service = Service()
    run = _Run(StartRunRequest(job_url="https://example.com/job"), "run-1")
    sink = _GraphSink(service, run)  # type: ignore[arg-type]
    trace = FrameworkTraceEvent(
        "values",
        "values",
        {"data": {"auth_status": "authenticated", "auth_model_id": "provider/model"}},
        {},
    )

    await sink.accept(trace)
    await sink.accept(trace)
    await sink.accept(FrameworkTraceEvent("updates", "orchestrator", {"data": {}}, {}))

    assert service.events == ["graph.event"]
    assert run.view.current_model == "provider/model"


@pytest.mark.asyncio
async def test_graph_sink_attributes_tool_action_to_selected_agent_model() -> None:
    class Service:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        async def _emit(
            self,
            run: object,
            event_type: str,
            payload: dict[str, object],
            **kwargs: object,
        ) -> None:
            del run, kwargs
            self.events.append((event_type, payload))

    service = Service()
    run = _Run(StartRunRequest(job_url="https://example.com/job"), "run-1")
    sink = _GraphSink(service, run)  # type: ignore[arg-type]

    await sink.accept(
        FrameworkTraceEvent(
            "model_selected",
            "AnswerWriter",
            {"model_id": "provider/answer-model", "role": "AnswerWriter"},
            {},
        )
    )
    await sink.accept(
        FrameworkTraceEvent(
            "agent_tool_start",
            "AnswerWriter:invocation-id",
            {"tool_name": "ask_human", "input": {}},
            {},
        )
    )

    assert service.events[-1] == (
        "tool.started",
        {
            "tool_name": "ask_human",
            "input": {},
            "model_id": "provider/answer-model",
        },
    )


async def _append(items: list[object], item: object) -> None:
    items.append(item)
