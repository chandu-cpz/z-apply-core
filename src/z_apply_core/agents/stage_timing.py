"""Per-stage model-call phase timing between tool and model turns.

Run history showed 58-96s silent gaps upstream of the router on every turn. This
wrapper times each middleware stage's ``awrap_model_call`` and emits a
``model_phase`` event whenever a stage crosses a duration threshold, making the
sink visible in the run event stream without changing any behavior.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ResponseT,
)

from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_PHASE_THRESHOLD_MS", "StageTimingMiddleware", "unwrap_stage_timing"]

#: Stages faster than this are normal model-call overhead; emitting their
#: timings would drown the stream. The inter-turn hole is 58_000ms+.
DEFAULT_PHASE_THRESHOLD_MS = 250


class StageTimingMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Time one wrapped middleware stage; emit ``model_phase`` when slow."""

    def __init__(
        self,
        inner: AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT],
        *,
        sink: FrameworkEventSink | None,
        role: str,
        threshold_ms: int = DEFAULT_PHASE_THRESHOLD_MS,
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._role = role
        self._threshold_ms = threshold_ms

    @property
    def name(self) -> str:
        return type(self._inner).__name__

    def __getattr__(self, item: str) -> Any:
        # Delegate everything else (state_schema, tools, other hooks) so the
        # wrapper is transparent to stack assembly and langgraph.
        if item.startswith("_"):
            raise AttributeError(item)
        return getattr(self._inner, item)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> Any:
        started = time.monotonic()
        try:
            return await self._inner.awrap_model_call(request, handler)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            if duration_ms >= self._threshold_ms:
                self._emit(duration_ms)

    def _emit(self, duration_ms: int) -> None:
        if self._sink is None:
            return
        try:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.info(
                    "model_phase %s %dms (no loop; dropped)",
                    self.name,
                    duration_ms,
                )
                return
            loop.create_task(
                self._sink.accept(
                    FrameworkTraceEvent(
                        event="model_phase",
                        name=self.name,
                        data={"role": self._role, "duration_ms": duration_ms},
                        raw={},
                    )
                )
            )
        except Exception:  # noqa: BLE001 - observability must never break the run
            logger.warning("model_phase emission failed", exc_info=True)


def unwrap_stage_timing(middleware: Any) -> Any:
    """Return the underlying middleware, looking through timing wrappers."""
    while isinstance(middleware, StageTimingMiddleware):
        middleware = middleware._inner  # noqa: SLF001 - structural unwrap
    return middleware


def wrap_chain_with_stage_timing(
    chain: list[AgentMiddleware[Any, Any, Any]],
    *,
    sink: FrameworkEventSink | None,
    role: str,
    threshold_ms: int = DEFAULT_PHASE_THRESHOLD_MS,
) -> list[AgentMiddleware[Any, Any, Any]]:
    """Wrap every stage that does its work in ``awrap_model_call``."""
    wrapped: list[AgentMiddleware[Any, Any, Any]] = []
    for middleware in chain:
        if "awrap_model_call" in type(middleware).__dict__:
            wrapped.append(
                StageTimingMiddleware(middleware, sink=sink, role=role, threshold_ms=threshold_ms)
            )
        else:
            wrapped.append(middleware)
    return wrapped
