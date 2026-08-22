"""DEC-010 dark-instrumentation regression: model_phase must reach the run sink.

The emitter (StageTimingMiddleware) worked, but service.accept() dropped
"model_phase" because _typed_framework_event mapped it to "graph.event" and
unknown graph events are filtered. These tests pin BOTH ends:
1. Wiring-level: build_orchestrator_middleware with a collecting sink + a slow
   CapabilityContext browser → model_phase arrives AT THE SINK.
2. Service-level: FrameworkTraceEvent("model_phase") survives
   FrameworkEventSinkAdapter.accept as a typed model.phase live event.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from z_apply_core.agents.goal_runner import ActiveGoalMiddleware
from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.orchestrator import build_orchestrator_middleware
from z_apply_core.agents.router_middleware import build_router_middleware
from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext
from z_apply_core.integrations.service import _GraphSink
from z_apply_core.memory.platform_playbooks import PlatformPlaybooks
from z_apply_core.stream_events import FrameworkTraceEvent, SequencedEventSink


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[FrameworkTraceEvent] = []

    async def accept(self, event: FrameworkTraceEvent) -> None:
        self.events.append(event)


class _StubObservation:
    def __init__(self) -> None:
        self.revision = 1
        self.signature = "sig"
        self.url = "https://example.test/x"
        self.title = "stub"
        self.evidence = "evidence " * 100

    def bounded_render(self, budget_chars: int = 0) -> str:
        return self.evidence

    def render(self) -> str:
        return self.evidence


class _SlowBrowser:
    """Browser whose capability inspection crosses the phase threshold."""

    pending_atomic_upload_target = None

    def __init__(self, caps: Any = None) -> None:
        self.current_observation = _StubObservation()
        self._caps = caps

    async def inspect_capabilities(self):
        await asyncio.sleep(0.35)
        return self._caps


def test_model_phase_reaches_production_chain_sink(tmp_path: Path) -> None:
    collecting = _CollectingSink()
    sink = SequencedEventSink(collecting, run_id="probe")
    provider = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))

    class _Provider:
        model_id = "fake"

        def get_model(self, thinking_effort=None):
            return provider

    router = build_router_middleware(_Provider(), role="orchestrator", sink=sink)
    chain = build_orchestrator_middleware(
        provider=_Provider(),
        run_context=RunContext(run_id="probe"),
        evidence_store=EvidenceStore(tmp_path),
        event_sink=sink,
        active_browser=_SlowBrowser(),  # type: ignore[arg-type]
        platform_playbooks=PlatformPlaybooks(),
        job_url="https://example.test/apply",
        context_inbox=None,
        router_middleware=router,
        orchestrator_human_guard=HumanEscalationGuardMiddleware(
            allowed_reasons=frozenset({"human_challenge"})
        ),
        active_goal_middleware=ActiveGoalMiddleware(
            is_terminal=lambda: False,
            on_no_progress=router.reject_active_response,
            sink=sink,
        ),
    )

    request = ModelRequest(
        model=provider,
        messages=[HumanMessage(content="probe")],
        tools=[],
    )

    async def terminal_handler(req):
        return "done"

    async def drive():
        result: Any = request
        for middleware in chain:
            result = await middleware.awrap_model_call(result, terminal_handler)
            if isinstance(result, str):
                break
        await asyncio.sleep(0.05)  # let create_task emissions flush

    asyncio.run(drive())
    phases = [event for event in collecting.events if event.event == "model_phase"]
    assert phases, "no model_phase events reached the production chain sink"
    slowest = max(phases, key=lambda e: e.data["duration_ms"])
    assert slowest.data["duration_ms"] >= 250
    assert slowest.name == "CapabilityContextMiddleware"


class _RecordingService:
    def __init__(self) -> None:
        self.live: list[tuple[str, dict[str, Any]]] = []
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def _emit_live(self, run, event_type, payload, source=None):
        self.live.append((event_type, payload))

    async def _emit(self, run, event_type, payload, source=None):
        self.emitted.append((event_type, payload))


def test_service_adapter_maps_model_phase_instead_of_dropping() -> None:
    from types import SimpleNamespace

    service = _RecordingService()
    run = SimpleNamespace(
        view=None,
        task=None,
        retention_release=None,
        done=None,
        human_requests={},
        artifacts=[],
        context_inbox=None,
        human_broker=None,
        call_ledger=None,
    )
    adapter = _GraphSink(service, run)

    event = FrameworkTraceEvent(
        event="model_phase",
        name="CapabilityContextMiddleware",
        data={"role": "orchestrator", "duration_ms": 61000},
        raw={},
    )
    asyncio.run(adapter.accept(event))

    # model.phase is NOT live-only: it must land on the persisted event path.
    assert service.emitted and service.emitted[-1][0] == "model.phase"
    assert service.emitted[-1][1]["duration_ms"] == 61000
    assert not any(live_type == "model.phase" for live_type, _ in service.live)


def test_capability_probe_reaches_sink_and_persists(tmp_path: Path) -> None:
    from z_apply_core.agents.capability_context import CapabilityContextMiddleware

    collecting = _CollectingSink()
    from z_apply_core.browser_observation import BrowserCapabilities as _Caps

    middleware = CapabilityContextMiddleware(
        _SlowBrowser(_Caps()),
        evidence_store=EvidenceStore(tmp_path),
        event_sink=SequencedEventSink(collecting, run_id="probe"),
        role="orchestrator",
    )

    async def handler(req):
        return "done"

    probe_request = ModelRequest(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
        messages=[HumanMessage(content="probe")],
        tools=[],
    )
    asyncio.run(middleware.awrap_model_call(probe_request, handler))
    await_flush = asyncio.sleep(0.05)
    asyncio.run(await_flush)

    probes = [e for e in collecting.events if e.event == "capability_probe"]
    assert probes, "capability_probe never reached the sink"
    probe = probes[-1]
    assert probe.data["controls_scanned"] >= 0
    assert probe.data["result_hash"] != "?"
    assert isinstance(probe.data["injected"], bool)

    # And the adapter persists it as capability.probe (not live-only).
    from types import SimpleNamespace

    service = _RecordingService()
    run = SimpleNamespace(
        view=None,
        task=None,
        retention_release=None,
        done=None,
        human_requests={},
        artifacts=[],
        context_inbox=None,
        human_broker=None,
        call_ledger=None,
    )
    adapter = _GraphSink(service, run)
    asyncio.run(adapter.accept(probe))
    persisted = [etype for etype, _ in service.emitted]
    assert "capability.probe" in persisted
