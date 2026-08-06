from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage


def replace_tool_calls(message: Any, tool_calls: Sequence[Mapping[str, Any]]) -> Any:
    """Return an assistant message copy with its tool calls replaced.

    Passes the message through unchanged when it has no tool calls or the
    replacement equals the current calls.
    """
    if not isinstance(message, AIMessage) or not message.tool_calls:
        return message
    replacement = list(tool_calls)
    if replacement == message.tool_calls:
        return message
    return message.model_copy(update={"tool_calls": replacement})


def rewrite_tool_calls(
    message: Any,
    rewrite: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> Any:
    """Return an assistant message copy with each tool call rewritten."""
    if not isinstance(message, AIMessage) or not message.tool_calls:
        return message
    return replace_tool_calls(message, [rewrite(call) for call in message.tool_calls])
