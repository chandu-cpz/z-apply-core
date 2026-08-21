"""Wire-level adaptations for the opencode Zen gateway (opencode.ai/zen/go).

Two problems this module solves, both gateway contract requirements rather
than preferences:

1. DeepSeek V4 models run in thinking mode by default and reject any forced
   ``tool_choice`` ("required", "any", and dict forms) with HTTP 400
   ``Thinking mode does not support this tool_choice``. LangChain's
   structured-output strategy forces ``tool_choice="any"`` to pin the schema
   tool, so ``ZenGatewayChat`` relaxes forced selection to "auto" (the
   documented DeepSeek ``supportsToolChoice: false`` workaround).

2. The gateway rejects an assistant message whose ``tool_call_ids`` lack
   following tool messages, and rejects a message with neither content nor
   tool_calls. Framework history can legitimately contain both (dropped
   parallel tool results, interrupted batches). ``prune_incomplete_tool_flow``
   fixes the outbound payload only; framework history stays byte-stable so
   the gateway prefix cache keeps hitting.

Prefix-cache breakpoints are stamped on the outbound payload (where
assistant and tool blocks survive serialization). The gateway auto-caches
request prefixes with only a ~5 minute TTL; ``prompt_cache_key`` +
``prompt_cache_retention`` (set by the gateway config) persist the cache
across gaps, and ``cache_control`` breakpoints mark exactly which points to
cache: up to 2 system messages, the last 2 user/assistant messages, and the
last tool message. ``ttl: "1h"`` is the documented ceiling.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import Any, cast

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

_CACHE_MARKER: dict[str, Any] = {"type": "ephemeral", "ttl": "1h"}


def _message_has_text(message: dict[str, Any]) -> bool:
    """True when an outbound message carries any non-empty text content."""
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip()
            for block in content
        )
    return False


def _strip_cache_control(message: dict[str, Any]) -> None:
    """Remove stale ``cache_control`` markers from one outbound message."""
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict):
            block.pop("cache_control", None)


def _stamp_cache_control(message: dict[str, Any]) -> None:
    """Stamp one breakpoint on the first text block of an outbound message.

    Empty text (for example an assistant message that only carries tool calls)
    carries no tokens worth caching, so it is left unstamped.
    """
    content = message.get("content")
    if isinstance(content, str):
        if not content.strip():
            return
        message["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": dict(_CACHE_MARKER),
            }
        ]
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                if not block["text"].strip():
                    continue
                block["cache_control"] = dict(_CACHE_MARKER)
                return


def stamp_cache_breakpoints(payload: dict[str, Any]) -> None:
    """Stamp prefix-cache breakpoints on an outbound payload.

    Stale markers from an earlier call are stripped first so breakpoints land
    exactly where this call wants them.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if isinstance(message, dict):
            _strip_cache_control(message)
    system_indices = [
        i
        for i, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "system"
    ][:2]
    tail_indices = [
        i
        for i, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
    ][-2:]
    tool_indices = [
        i
        for i, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "tool"
    ][-1:]
    for index in system_indices + tail_indices + tool_indices:
        _stamp_cache_control(messages[index])


def prune_incomplete_tool_flow(
    payload: dict[str, Any],
    source_messages: Sequence[Any],
) -> None:
    """Drop orphan tool results and unsatisfied tool calls from the payload.

    Rebuilds the outbound message list, pairing each tool result to its
    pending assistant tool call by id, skipping orphan results on the wire
    only, and pruning assistant tool_calls that never produced a result
    message. Framework history stays byte-stable so the gateway prefix cache
    keeps hitting (deleting orphans from history broke the prefix for every
    later turn). Pruning is required by the gateway contract: an assistant
    message whose tool_call_ids lack following tool messages is rejected with
    HTTP 400, which the retry policy does not retry.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    # The pruning walk pairs converted dicts with source messages 1:1. If a
    # langchain version ever changes that correspondence, skip pruning (and
    # log loudly) rather than raising mid-request; the gateway rejects
    # genuinely malformed flows and the retry policy owns recovery.
    if len(messages) != len(source_messages):
        logger.warning(
            "opencodego wire: message/source length mismatch (%d vs %d); "
            "skipping tool-flow pruning",
            len(messages),
            len(source_messages),
        )
        return
    pending_assistant: dict[str, Any] | None = None
    pending_call_ids: set[str] = set()
    pending_calls: list[dict[str, Any]] = []
    satisfied: set[str] = set()
    outbound: list[dict[str, Any]] = []

    def close_pending_assistant() -> None:
        nonlocal pending_assistant, pending_calls, pending_call_ids, satisfied
        if pending_assistant is not None:
            calls = pending_assistant.get("tool_calls")
            if isinstance(calls, list):
                kept = [call for call in calls if call.get("id") in satisfied]
                if kept:
                    pending_assistant["tool_calls"] = kept
                elif _message_has_text(pending_assistant):
                    # Text-only assistant message: drop the dead tool_calls,
                    # keep the text.
                    pending_assistant.pop("tool_calls", None)
                    pending_assistant.pop("reasoning_content", None)
                else:
                    # No tool result survived and the assistant carried no
                    # text: the gateway rejects a message with neither
                    # content nor tool_calls, so drop the empty message.
                    for index, existing in enumerate(outbound):
                        if existing is pending_assistant:
                            del outbound[index]
                            break
        pending_assistant = None
        pending_calls = []
        pending_call_ids = set()
        satisfied = set()

    for message, source in zip(messages, source_messages, strict=True):
        role = message.get("role")
        if isinstance(source, AIMessage) and role == "assistant":
            close_pending_assistant()
            if message.get("tool_calls"):
                reasoning = source.additional_kwargs.get("reasoning_content")
                message["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""
                pending_assistant = message
                pending_calls = list(message["tool_calls"])
                pending_call_ids = set()
                for pending in pending_calls:
                    pending_id = pending.get("id")
                    if isinstance(pending_id, str) and pending_id:
                        pending_call_ids.add(pending_id)
                for call, source_call in zip(pending_calls, source.tool_calls, strict=False):
                    if not call.get("id") and source_call.get("id"):
                        call["id"] = source_call["id"]
                        pending_call_ids.add(call["id"])
                satisfied = set()
            else:
                pending_assistant = None
                pending_calls = []
                pending_call_ids = set()
                satisfied = set()
        elif role == "tool":
            tool_id = message.get("tool_call_id")
            if (
                pending_assistant is None
                or not isinstance(tool_id, str)
                or tool_id not in pending_call_ids
            ):
                # Orphan tool result: no pending call references it. Skip it
                # on the wire only; framework history keeps it.
                continue
            pending_call_ids.discard(tool_id)
            satisfied.add(tool_id)
            matched_call: dict[str, Any] | None = None
            for candidate in pending_calls:
                if candidate.get("id") == tool_id:
                    matched_call = candidate
                    break
            if matched_call is not None:
                call_id = matched_call.get("id") or ""
                if tool_id and not call_id:
                    matched_call["id"] = tool_id
                elif call_id and not tool_id:
                    message["tool_call_id"] = call_id
        else:
            close_pending_assistant()
        outbound.append(message)
    close_pending_assistant()
    messages[:] = outbound


def _dump_payload(payload: dict[str, Any]) -> None:
    """Write one outbound payload to Z_APPLY_PAYLOAD_DUMP when set (debug)."""
    dump_dir = os.environ.get("Z_APPLY_PAYLOAD_DUMP")
    if not dump_dir:
        return
    import json
    import time
    from pathlib import Path

    Path(dump_dir).mkdir(parents=True, exist_ok=True)
    Path(dump_dir, f"{time.time()}.json").write_text(json.dumps(payload, default=str))


class ZenGatewayChat:
    """Mixin adapting ChatDeepSeek to the opencode Zen gateway.

    Relaxes forced tool selection to "auto" (thinking mode rejects anything
    else with HTTP 400), surfaces the gateway-reported cost, prunes
    incomplete tool flows, and stamps prefix-cache breakpoints on every
    outbound payload.
    """

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: dict[str, Any] | str | bool | None = None,
        strict: bool | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        if isinstance(tool_choice, dict) or tool_choice in {"any", "required"}:
            tool_choice = "auto"
        return super().bind_tools(  # type: ignore[misc,no-any-return]
            tools,
            tool_choice=tool_choice,
            strict=strict,
            parallel_tool_calls=parallel_tool_calls,
            **kwargs,
        )

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict[str, Any] | None = None,
    ) -> Any:
        result = super()._create_chat_result(response, generation_info)  # type: ignore[misc]
        raw = getattr(response, "cost", None)
        if raw is None:
            model_extra = getattr(response, "model_extra", None) or {}
            raw = model_extra.get("cost") if isinstance(model_extra, dict) else None
        if raw is not None and result.generations:
            result.generations[0].message.additional_kwargs["cost"] = raw
        return result

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)  # type: ignore[misc]
        messages = payload.get("messages")
        if isinstance(messages, list):
            source_messages: list[Any] = self._convert_input(input_).to_messages()  # type: ignore[attr-defined]
            prune_incomplete_tool_flow(payload, source_messages)
            stamp_cache_breakpoints(payload)
        _dump_payload(payload)
        return cast(dict[str, Any], payload)
