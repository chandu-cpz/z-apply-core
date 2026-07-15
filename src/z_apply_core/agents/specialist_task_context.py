from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState, ContextT, ModelResponse, ResponseT
from langchain_core.messages import BaseMessage, HumanMessage

SPECIALIST_TASK_CONTEXT_SOURCE = "specialist_task_controller"


class SpecialistTaskContextMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Keep one delegated specialist objective explicit across routed tool turns."""

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        task = initial_specialist_task(request.messages)
        if not task:
            return await handler(request)
        reminder = HumanMessage(
            name=SPECIALIST_TASK_CONTEXT_SOURCE,
            additional_kwargs={"lc_source": SPECIALIST_TASK_CONTEXT_SOURCE},
            content=(
                "ACTIVE SPECIALIST TASK\n"
                f"{task}\n"
                "Complete only this delegated task. Preserve its exact field, gate, "
                "constraints, and visible options across tool results and model rotation."
            ),
        )
        return await handler(request.override(messages=[*request.messages, reminder]))


def initial_specialist_task(messages: Sequence[BaseMessage]) -> str:
    """Return the immutable task message DeepAgents places first in subagent state."""
    for message in messages:
        if isinstance(message, HumanMessage) and message.name is None:
            return message.text.strip()
    return ""
