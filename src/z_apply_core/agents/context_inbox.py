from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState, ContextT, ResponseT
from langchain_core.messages import HumanMessage

CONTEXT_MESSAGE_SOURCE = "z_apply_live_context"


@dataclass(frozen=True, slots=True)
class ContextMessage:
    content: str
    source: str


class ContextInbox:
    """Bounded per-run inbox for guidance arriving during an active agent run."""

    def __init__(self, *, max_pending: int = 32) -> None:
        self._queue: asyncio.Queue[ContextMessage] = asyncio.Queue(maxsize=max_pending)

    def put(self, message: ContextMessage) -> None:
        self._queue.put_nowait(message)

    def drain(self) -> tuple[ContextMessage, ...]:
        messages: list[ContextMessage] = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return tuple(messages)


class ContextInboxMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Append externally supplied guidance at a supported model boundary.

    DeepAgents invocation context is immutable for an invocation. Middleware
    state updates are the native way to add messages while preserving the same
    active graph thread and checkpoint.
    """

    def __init__(self, inbox: ContextInbox) -> None:
        super().__init__()
        self._inbox = inbox

    async def abefore_model(
        self,
        state: AgentState[ResponseT],
        runtime: Any,
    ) -> dict[str, Any] | None:
        del state, runtime
        pending = self._inbox.drain()
        if not pending:
            return None
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "LIVE USER CONTEXT: Apply this new information to the active "
                        "application. It updates guidance but does not prove that any "
                        "browser action has completed.\n\n" + message.content
                    ),
                    name=CONTEXT_MESSAGE_SOURCE,
                    additional_kwargs={
                        "lc_source": CONTEXT_MESSAGE_SOURCE,
                        "responder": message.source,
                    },
                )
                for message in pending
            ]
        }
