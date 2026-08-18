from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from z_apply_core.browser_tools import BROWSER_CHANGING_TOOL_NAMES
from z_apply_core.stream_events import FrameworkEventSink, FrameworkTraceEvent

logger = logging.getLogger(__name__)


class SerializeBrowserMutationsMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Execute parallel browser mutations one at a time within a response.

    Multiple browser-changing tool calls in one model response are allowed and
    kept, but they must never run concurrently on a single page: element refs
    are positional and re-assigned on every SPA re-render, and simultaneous
    writes can interleave keystrokes or race a shifting DOM (observed as crossed
    and concatenated values). This middleware serializes every browser mutation
    through a per-agent lock so each write completes and re-renders before the
    next one starts, while read-only calls stay parallel.
    """

    def __init__(
        self,
        *,
        sink: FrameworkEventSink | None = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        super().__init__()
        self._sink = sink
        # A caller may share one lock across the orchestrator and every specialist
        # so browser mutations never overlap even when a subagent and the
        # orchestrator act in the same response.
        self._mutation_lock = lock if lock is not None else asyncio.Lock()
        self._batch_mutation_ids: set[str] = set()
        self._batch_failed = False

    @property
    def name(self) -> str:
        return "SerializeBrowserMutationsMiddleware"

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        self._batch_mutation_ids = set()
        self._batch_failed = False
        result = await handler(request)
        mutation_names = [
            str(call.get("name", ""))
            for message in result.result
            if isinstance(message, AIMessage)
            for call in (message.tool_calls or [])
            if str(call.get("name", "")) in BROWSER_CHANGING_TOOL_NAMES
        ]
        if len(mutation_names) > 1:
            logger.info(
                "Serializing %d browser mutation(s) in one response: %s",
                len(mutation_names),
                ", ".join(mutation_names),
            )
            await self._emit_serialized(mutation_names)
        self._batch_mutation_ids = {
            str(call.get("id", ""))
            for message in result.result
            if isinstance(message, AIMessage)
            for call in (message.tool_calls or [])
            if str(call.get("name", "")) in BROWSER_CHANGING_TOOL_NAMES
        }
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        name = str(request.tool_call.get("name", ""))
        if name not in BROWSER_CHANGING_TOOL_NAMES:
            return await handler(request)
        call_id = str(request.tool_call.get("id", ""))
        if call_id in self._batch_mutation_ids and self._batch_failed:
            return ToolMessage(
                content=(
                    "SKIPPED: an earlier browser mutation in this same response "
                    "failed, so this mutation was not executed. The page may have "
                    "re-rendered or navigated. Capture fresh browser evidence "
                    "(browser_snapshot) before continuing."
                ),
                tool_call_id=call_id,
                name=name,
                status="error",
            )
        async with self._mutation_lock:
            result = await handler(request)
        if (
            call_id in self._batch_mutation_ids
            and isinstance(result, ToolMessage)
            and result.status == "error"
        ):
            self._batch_failed = True
            logger.warning(
                "browser mutation %s failed; skipping remaining %d mutation(s) in this response",
                name,
                len(self._batch_mutation_ids),
            )
        return result

    async def _emit_serialized(self, mutations: list[str]) -> None:
        if self._sink is None:
            return
        await self._sink.accept(
            FrameworkTraceEvent(
                event="mutation_serialized",
                name="orchestrator",
                data={
                    "middleware": "SerializeBrowserMutationsMiddleware",
                    "count": len(mutations),
                    "mutations": mutations,
                },
                raw={},
            )
        )
