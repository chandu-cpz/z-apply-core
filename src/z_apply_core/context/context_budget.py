"""Shared token-estimation helper for the runtime.

The old ``ContextBudgetMiddleware`` lived here and dropped messages from the
model-facing history to enforce a hard token budget. It was removed: dropping
``ToolMessage`` responses while keeping the assistant ``tool_calls`` message
produced invalid histories that the DeepSeek-compatible gateway rejected with
``400 invalid_request_error``, and any mid-conversation message removal broke
prompt-cache prefixes on the paid gateway. DeepAgents/LangGraph manage context
with their own defaults; the runtime only measures usage.
"""


def estimate_tokens(text: str) -> int:
    """Coarse token estimate (chars / 4), used only for usage reporting."""
    return max(1, len(text) // 4)
