"""FAIL-006 addendum probe: per-stage monotonic timing of the REAL orchestrator
middleware chain between tool completion and router emission.

Wraps every middleware instance from build_orchestrator_middleware (+ the
deepagents stack pieces that sit outside it) with a timer, then pushes one
model request with an attempt-7-sized history (~15K tokens) through the chain.
Whatever eats 58-96s in production shows up here.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ResponseT,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from z_apply_core.agents.human_escalation_guard import HumanEscalationGuardMiddleware
from z_apply_core.agents.middleware_factory import build_agent_middleware
from z_apply_core.agents.router_middleware import build_router_middleware
from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext
from z_apply_core.memory.platform_playbooks import PlatformPlaybooks
from z_apply_core.stream_events import SequencedEventSink

timings: list[tuple[str, float]] = []


class TimingWrapper(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"{type(self._inner).__name__}"

    async def awrap_model_call(self, request, handler):
        t0 = time.monotonic()
        try:
            return await self._inner.awrap_model_call(request, handler)
        finally:
            timings.append((self.name, time.monotonic() - t0))


from z_apply_core.browser_observation import BrowserObservation as _RealObs  # noqa: E402


def _stub_obs() -> _RealObs:
    return _RealObs.create(
        revision=1,
        url="https://example.test/x",
        title="stub",
        evidence="stub evidence " * 200,
    )


class _StubBrowser:
    pending_atomic_upload_target = None
    _obs = _stub_obs()

    @property
    def current_observation(self):
        return self._obs

    async def inspect_capabilities(self):
        # Landing-page measured cost; form pages cost more (see probe_capability_timing).
        await asyncio.sleep(0.4)
        return None


class _FakeProvider:
    def __init__(self) -> None:
        self.model_id = "fake"

    def get_model(self, thinking_effort=None):
        return GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))


async def main() -> None:
    run_context = RunContext(run_id="probe")
    from pathlib import Path as _P

    store = EvidenceStore(_P("/tmp/fail006-probe"))
    sink = SequencedEventSink(None, run_id="probe")
    provider = _FakeProvider()
    router = build_router_middleware(provider, role="orchestrator", sink=sink)

    chain = build_agent_middleware(
        role="orchestrator",
        provider=provider,
        run_context=run_context,
        evidence_store=store,
        event_sink=sink,
        active_browser=_StubBrowser(),
        platform_playbooks=PlatformPlaybooks(),
        job_url="https://jobs.smartrecruiters.com/TheNielsenCompany/x",
        context_inbox=None,
        router_middleware=router,
        human_guard=HumanEscalationGuardMiddleware(allowed_reasons=frozenset({"human_challenge"})),
        no_progress_kwargs={
            "max_stagnant_tool_calls": 12,
            "max_identical_denials": 3,
            "max_non_progress": 6,
            "window_size": 6,
            "repetition_threshold": 3,
        },
    )
    wrapped = [TimingWrapper(m) for m in chain]

    # ~15K-token history like attempt 7's healthy turns.
    filler = "form field value x" * 800
    messages: list[Any] = [HumanMessage(content="Apply to this job. " + filler)]
    for i in range(12):
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "browser_click", "args": {"target": f"e{i}"}, "id": f"c{i}"}],
            )
        )
        messages.append(
            ToolMessage(content=f"[receipt rev={i}] {filler[:2000]}", tool_call_id=f"c{i}")
        )

    # Tool schemas roughly production-shaped (17 tools).
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": "x " * 200,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for i in range(17)
    ]

    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    request = ModelRequest(model=model, messages=messages, tools=tools)

    async def terminal_handler(req):
        return "REACHED-MODEL"

    t0 = time.monotonic()
    result = request
    for mw in wrapped:
        result = await mw.awrap_model_call(result, terminal_handler)
        if result == "REACHED-MODEL":
            break
    wall = time.monotonic() - t0

    print(f"total chain wall clock: {wall:.2f}s")
    for name, dt in timings:
        bar = "#" * min(60, int(dt * 20))
        print(f"  {name:38s} {dt * 1000:9.1f} ms  {bar}")
    print(f"\nreached model: {result == 'REACHED-MODEL'}")


asyncio.run(main())
