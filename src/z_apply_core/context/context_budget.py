from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import AnyMessage, BaseMessage, SystemMessage, ToolMessage

from z_apply_core.context.evidence_store import EvidenceStore
from z_apply_core.context.run_context import RunContext

_LONG_TOOL_MESSAGE_CHARS = 2000
_TRUNCATED_WITH_REVISION = (
    "[evidence truncated — full revision {revision} in evidence store; use browser_find]"
)
_TRUNCATED_GENERIC = "[tool result truncated to fit context budget]"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    total = 0
    for message in messages:
        content = message.content
        if content is not None:
            total += len(content)
    return total


class ContextBudgetMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Keep the model-facing message list inside a hard token budget."""

    def __init__(
        self,
        *,
        budget_tokens: int = 120000,
        keep_tool_messages: int = 8,
        evidence_store: EvidenceStore | None = None,
        run_context: RunContext | None = None,
    ) -> None:
        super().__init__()
        self._budget_tokens = budget_tokens
        self._keep_tool_messages = keep_tool_messages
        self._evidence_store = evidence_store
        self._run_context = run_context

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        tool_messages = [
            message for message in request.messages if isinstance(message, ToolMessage)
        ]
        if (
            estimate_messages_tokens(request.messages) <= self._budget_tokens
            and len(tool_messages) < self._keep_tool_messages
        ):
            return await handler(request)
        compacted = self._compact_messages(request.messages)
        return await handler(request.override(messages=compacted, tools=request.tools))

    def _compact_messages(self, messages: list[AnyMessage]) -> list[AnyMessage]:
        tool_indices = [
            index for index, message in enumerate(messages) if isinstance(message, ToolMessage)
        ]
        keep_count = min(len(tool_indices), self._keep_tool_messages)
        retained_tool_indices = set(tool_indices[len(tool_indices) - keep_count :])
        system_indices = [
            index for index, message in enumerate(messages) if isinstance(message, SystemMessage)
        ]
        retained_system_indices = {system_indices[-1]} if system_indices else set()

        compacted: list[AnyMessage] = []
        for index, message in enumerate(messages):
            if isinstance(message, ToolMessage):
                if index not in retained_tool_indices:
                    continue
                compacted.append(self._project_tool_message(message))
            elif isinstance(message, SystemMessage):
                if index not in retained_system_indices:
                    continue
                compacted.append(message)
            else:
                compacted.append(message)
        return compacted

    def _project_tool_message(self, message: ToolMessage) -> ToolMessage:
        content = message.content
        if content is None or len(content) <= _LONG_TOOL_MESSAGE_CHARS:
            return message
        revision = message.additional_kwargs.get("browser_revision")
        if isinstance(revision, int):
            replacement = _TRUNCATED_WITH_REVISION.format(revision=revision)
        else:
            replacement = _TRUNCATED_GENERIC
        return ToolMessage(
            content=replacement,
            tool_call_id=message.tool_call_id,
            name=message.name,
            status=message.status,
            id=message.id,
            additional_kwargs=message.additional_kwargs,
        )
