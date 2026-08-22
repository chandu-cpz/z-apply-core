"""Per-stage phase timing + capability change capture tests.

These instruments exist to name the inter-turn sink (58-96s silent
gaps upstream of the router) on the next instrumented run.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ResponseT,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from z_apply_core.agents.stage_timing import (
    DEFAULT_PHASE_THRESHOLD_MS,
    StageTimingMiddleware,
    unwrap_stage_timing,
    wrap_chain_with_stage_timing,
)
from z_apply_core.stream_events import FrameworkTraceEvent


class _SlowMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def awrap_model_call(self, request, handler):
        await asyncio.sleep(self._delay)
        return await handler(request)


class _Sink:
    def __init__(self) -> None:
        self.events: list[FrameworkTraceEvent] = []

    async def accept(self, event: FrameworkTraceEvent) -> None:
        self.events.append(event)


def _request() -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
        messages=[HumanMessage(content="probe")],
        tools=[],
    )


def test_slow_stage_emits_model_phase_event() -> None:
    sink = _Sink()
    middleware = StageTimingMiddleware(
        _SlowMiddleware(0.05), sink=sink, role="orchestrator", threshold_ms=10
    )

    async def handler(req):
        return "done"

    result = asyncio.run(middleware.awrap_model_call(_request(), handler))
    assert result == "done"
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event == "model_phase"
    assert event.name == "_SlowMiddleware"
    assert event.data["role"] == "orchestrator"
    assert event.data["duration_ms"] >= 10


def test_fast_stage_stays_silent() -> None:
    sink = _Sink()
    middleware = StageTimingMiddleware(
        _SlowMiddleware(0.0),
        sink=sink,
        role="orchestrator",
        threshold_ms=DEFAULT_PHASE_THRESHOLD_MS,
    )

    async def handler(req):
        return "done"

    asyncio.run(middleware.awrap_model_call(_request(), handler))
    assert sink.events == []


def test_wrap_chain_only_wraps_awrap_stages_and_unwraps_cleanly() -> None:
    class AwrapStage(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
        async def awrap_model_call(self, request, handler):
            return await handler(request)

    class OtherStage(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
        async def abefore_model(self, state, runtime):  # noqa: ANN001
            return None

    chain: list[Any] = [AwrapStage(), OtherStage()]
    wrapped = wrap_chain_with_stage_timing(chain, sink=None, role="orchestrator")

    assert isinstance(wrapped[0], StageTimingMiddleware)
    assert not isinstance(wrapped[1], StageTimingMiddleware)
    assert unwrap_stage_timing(wrapped[0]) is chain[0]
    # Transparency: name delegation lets stack assembly and logs identify stages.
    assert wrapped[0].name == "AwrapStage"


def test_capability_change_detection_flags_thrash() -> None:
    from z_apply_core.agents.capability_context import CapabilityContextMiddleware
    from z_apply_core.browser_observation import BrowserCapabilities

    middleware = CapabilityContextMiddleware(None)
    first = BrowserCapabilities(auth_gate_visible=True)
    second = BrowserCapabilities(auth_gate_visible=False)

    assert middleware._note_capabilities(first) is False  # first observation: no baseline
    assert middleware._note_capabilities(first) is False  # unchanged
    assert middleware._note_capabilities(second) is True  # thrash detected
    assert middleware._note_capabilities(None) is False  # unavailable inspection never flags
