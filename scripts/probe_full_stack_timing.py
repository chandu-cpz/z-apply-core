"""Inter-turn hole probe v2: time EVERY middleware in the fully
assembled create_deep_agent stack — including deepagents-internal stages our
chain probe bypassed (Filesystem, SubAgent, Summarization count, PatchToolCalls,
prompt caching) — across multiple turns through the real langgraph loop.

Diagnostic only; runs entirely in-process with a scripted fake model.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ResponseT,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

stage_ms: dict[str, list[float]] = {}
turn_markers: list[tuple[str, float]] = []


def _instrument(cls: type, method: str) -> None:
    orig = getattr(cls, method)

    def wrapped(self: Any, *args: Any, **kwargs: Any):
        t0 = time.monotonic()
        try:
            return orig(self, *args, **kwargs)
        finally:
            stage_ms.setdefault(f"{cls.__name__}.{method}", []).append(
                (time.monotonic() - t0) * 1000
            )

    setattr(cls, method, wrapped)


class ScriptedToolCallModel(BaseChatModel):
    """Always requests one noop tool call; stops after N invocations."""

    _calls: int = PrivateAttr(default=0)
    max_calls: int = 4

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedToolCallModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._calls += 1
        turn_markers.append(("model_invoke", time.monotonic()))
        if self._calls >= self.max_calls:
            msg = AIMessage(content="done")
        else:
            msg = AIMessage(
                content="",
                tool_calls=[{"name": "noop", "args": {}, "id": f"c{self._calls}"}],
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])


from langchain_core.tools import tool  # noqa: E402


@tool
def noop() -> str:
    """Do nothing."""
    turn_markers.append(("tool_run", time.monotonic()))
    return "ok"


class TimingProbe(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Outermost probe: measures OUR whole-chain entry (post-deepagents)."""

    async def awrap_model_call(self, request: ModelRequest, handler):
        t0 = time.monotonic()
        try:
            return await handler(request)
        finally:
            stage_ms.setdefault("OUR_CHAIN_TOTAL", []).append((time.monotonic() - t0) * 1000)


async def main() -> None:
    # Instrument deepagents-internal middleware classes (diagnostic patch).
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from deepagents.middleware.subagents import SubAgentMiddleware
    from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware

    for cls in (
        FilesystemMiddleware,
        SubAgentMiddleware,
        _DeepAgentsSummarizationMiddleware,
        PatchToolCallsMiddleware,
    ):
        # Only patch methods the class defines ITSELF; wrapping inherited base
        # methods changes their binding semantics under langgraph.
        if "awrap_model_call" in cls.__dict__:
            _instrument(cls, "awrap_model_call")
        if "abefore_model" in cls.__dict__:
            _instrument(cls, "abefore_model")

    filler = "form field value x" * 900  # ~15K chars per snapshot receipt
    history = f"Apply to this job. {filler}"

    agent = create_deep_agent(
        model=ScriptedToolCallModel(max_calls=4),
        tools=[noop],
        system_prompt="probe",
        middleware=[TimingProbe()],
    )

    messages: list[Any] = [{"role": "user", "content": history}]
    t0 = time.monotonic()
    await agent.ainvoke({"messages": messages}, config={"recursion_limit": 30})
    wall = time.monotonic() - t0

    print(f"total graph wall clock ({len(turn_markers)} markers): {wall:.2f}s")
    print("\nper-stage totals (ms):")
    for stage, values in sorted(stage_ms.items(), key=lambda kv: -sum(kv[1])):
        print(
            f"  {stage:48s} n={len(values)} total={sum(values):9.1f} avg={sum(values) / len(values):8.1f}"
        )

    print("\nturn marker timeline (s since start):")
    base = turn_markers[0][1] if turn_markers else 0
    for kind, ts in turn_markers:
        print(f"  {kind:14s} +{ts - base:8.3f}s")


asyncio.run(main())
