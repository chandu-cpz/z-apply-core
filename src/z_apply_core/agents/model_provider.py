from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ModelCapabilities(BaseModel):
    tools: bool = False
    structured: bool = False
    vision: bool = False
    reasoning: bool = False


class ModelInfo(BaseModel):
    id: str
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    quality_hint: float = 0.5
    deprecated: bool = False
    model_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ModelSelection:
    info: ModelInfo
    llm: Any
    callback: Any | None = None


def _build_llm(
    chat_cls: type[Any],
    *,
    model: str,
    base_url: str,
    api_key: str,
    temperature: float | None = None,
    extra_body: dict[str, Any] | None = None,
    request_timeout: float | None = 180.0,
    **chat_kwargs: Any,
) -> Any:
    """Construct one OpenAI-compatible chat model with uniform core args.

    All provider chat classes (``ChatOpenAI``, ``ChatDeepSeek``, ``ChatGroq``)
    accept ``model``/``api_key``/``base_url`` under their public pydantic
    aliases, and vendor-specific options ride through ``extra_body`` or the
    class's own typed parameters.

    ``request_timeout`` bounds one model call. Without it every OpenAI-style
    SDK defaults to 600s, so a stalled gateway connection (seen with flaky
    ISPs and gateway backends) wedges the run silently for ten minutes before
    the retry middleware can recover. The outer retry policy turns a bounded
    timeout into retries with backoff instead of a silent hang.
    """
    from pydantic import SecretStr

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": SecretStr(api_key),
        "base_url": base_url,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if extra_body:
        kwargs["extra_body"] = extra_body
    if request_timeout is not None:
        kwargs["request_timeout"] = request_timeout
    kwargs.update(chat_kwargs)
    llm = chat_cls(**kwargs)
    from z_apply_core.context.model_metrics import attach_first_token_callback

    attach_first_token_callback(llm)
    return llm


def _selection(
    *,
    model_id: str,
    provider: str,
    pricing: str,
    quality_hint: float,
    llm: Any,
    tools: bool,
    structured: bool,
    vision: bool,
    reasoning: bool,
) -> ModelSelection:
    """Build the typed ModelSelection every provider lease returns."""
    info = ModelInfo(
        id=model_id,
        capabilities=ModelCapabilities(
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
        ),
        quality_hint=quality_hint,
        metadata={"provider": provider, "pricing": pricing},
    )
    return ModelSelection(info=info, llm=llm, callback=None)


@runtime_checkable
class ModelProvider(Protocol):
    async def lease(
        self,
        *,
        tools: bool = False,
        structured: bool = False,
        vision: bool = False,
        reasoning: bool = False,
        reasoning_effort: str | None = None,
        priority: str = "balanced",
        excluded_model_ids: frozenset[str] | None = None,
    ) -> ModelSelection: ...

    def record_failure(self, model_id: str, **kwargs: Any) -> None: ...

    def cooldown_model(self, model_id: str, seconds: float) -> None: ...


class AgnesProvider:
    """Free OpenAI-compatible provider. No scoring or rate-limit tracking."""

    BASE_URL = "https://apihub.agnes-ai.com/v1"
    DEFAULT_MODEL = "agnes-2.0-flash"

    def __init__(
        self,
        api_key: str = "",
        reasoning: bool = True,
        model: str = "",
    ) -> None:
        self._api_key = api_key or os.environ.get("AGNES_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "AGNES_API_KEY is required for AgnesProvider. "
                "Get a key at https://platform.agnes-ai.com/"
            )
        self._reasoning = reasoning
        self._model = model or os.environ.get("AGNES_MODEL", "") or self.DEFAULT_MODEL

    async def lease(
        self,
        *,
        tools: bool = False,
        structured: bool = False,
        vision: bool = False,
        reasoning: bool = False,
        reasoning_effort: str | None = None,
        priority: str = "balanced",
        excluded_model_ids: frozenset[str] | None = None,
    ) -> ModelSelection:
        from langchain_deepseek import ChatDeepSeek

        llm = _build_llm(
            ChatDeepSeek,
            model=self._model,
            base_url=self.BASE_URL,
            api_key=self._api_key,
            temperature=0.3,
            extra_body={"chat_template_kwargs": {"enable_thinking": self._reasoning}},
        )

        logger.info(
            "AgnesProvider: selected %s (tools=%s, vision=%s, reasoning=%s)",
            self._model,
            tools,
            vision,
            self._reasoning,
        )
        return _selection(
            model_id=self._model,
            provider="agnes",
            pricing="free",
            quality_hint=0.8,
            llm=llm,
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
        )

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        logger.debug("AgnesProvider: record_failure ignored for %s", model_id)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        logger.debug("AgnesProvider: cooldown ignored for %s", model_id)


class InferXProvider:
    """OpenAI-compatible provider for InferX hosted endpoints (model.inferx.net)."""

    BASE_URL = "https://model.inferx.net/endpoints/v1"
    DEFAULT_MODEL = "deepseek-v4-flash-0731"

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        reasoning: bool = True,
        reasoning_effort: str = "high",
    ) -> None:
        self._api_key = api_key or os.environ.get("INFERX_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "INFERX_API_KEY is required for InferXProvider. "
                "Create an inference key in the InferX Console (https://model.inferx.net/)."
            )
        self._model = model or os.environ.get("INFERX_MODEL", "") or self.DEFAULT_MODEL
        self._reasoning = reasoning
        self._reasoning_effort = reasoning_effort

    async def lease(
        self,
        *,
        tools: bool = False,
        structured: bool = False,
        vision: bool = False,
        reasoning: bool = False,
        reasoning_effort: str | None = None,
        priority: str = "balanced",
        excluded_model_ids: frozenset[str] | None = None,
    ) -> ModelSelection:
        from langchain_deepseek import ChatDeepSeek

        if self._reasoning:
            effort = self._reasoning_effort if self._reasoning_effort in {"high", "max"} else "high"
            extra_body: dict[str, Any] = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": effort,
            }
        else:
            extra_body = {"thinking": {"type": "disabled"}}

        llm = _build_llm(
            ChatDeepSeek,
            model=self._model,
            base_url=self.BASE_URL,
            api_key=self._api_key,
            temperature=0.3,
            extra_body=extra_body,
        )

        logger.info("InferXProvider: selected %s (tools=%s, vision=%s)", self._model, tools, vision)
        return _selection(
            model_id=self._model,
            provider="inferx",
            pricing="token",
            quality_hint=0.8,
            llm=llm,
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
        )

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        logger.debug("InferXProvider: record_failure ignored for %s", model_id)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        logger.debug("InferXProvider: cooldown ignored for %s", model_id)


class GroqProvider:
    """Groq hosted endpoints (api.groq.com) via the official langchain-groq package.

    Default model is Qwen3.6-27B, a 27B dense multimodal model with tool use,
    JSON mode, reasoning, and vision. Thinking mode maps to Groq's native
    ``reasoning_effort`` ("default" = thinking, "none" = non-thinking) and
    ``reasoning_format="parsed"`` so reasoning arrives in
    ``additional_kwargs["reasoning_content"]`` instead of inline think tags
    (Groq rejects ``raw`` format when tool use or JSON mode is enabled).
    """

    BASE_URL = "https://api.groq.com/openai/v1"
    DEFAULT_MODEL = "qwen/qwen3.6-27b"

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        reasoning: bool = True,
    ) -> None:
        self._api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "GROQ_API_KEY is required for GroqProvider. Get a key at https://console.groq.com/"
            )
        self._model = model or os.environ.get("GROQ_MODEL", "") or self.DEFAULT_MODEL
        self._reasoning = reasoning

    def _groq_chat_kwargs(self) -> dict[str, Any]:
        if self._model.startswith("llama-"):
            return {}
        if "gpt-oss" in self._model:
            return {"reasoning_effort": "medium" if self._reasoning else "none"}
        return {
            "reasoning_format": "parsed",
            "reasoning_effort": "default" if self._reasoning else "none",
        }

    async def lease(
        self,
        *,
        tools: bool = False,
        structured: bool = False,
        vision: bool = False,
        reasoning: bool = False,
        reasoning_effort: str | None = None,
        priority: str = "balanced",
        excluded_model_ids: frozenset[str] | None = None,
    ) -> ModelSelection:
        from langchain_groq import ChatGroq

        llm = _build_llm(
            ChatGroq,
            model=self._model,
            base_url=self.BASE_URL,
            api_key=self._api_key,
            temperature=0.6 if self._reasoning else 0.7,
            **self._groq_chat_kwargs(),
        )

        logger.info(
            "GroqProvider: selected %s (tools=%s, vision=%s, reasoning=%s)",
            self._model,
            tools,
            vision,
            self._reasoning,
        )
        return _selection(
            model_id=self._model,
            provider="groq",
            pricing="token",
            quality_hint=0.85,
            llm=llm,
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
        )

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        logger.debug("GroqProvider: record_failure ignored for %s", model_id)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        logger.debug("GroqProvider: cooldown ignored for %s", model_id)


class OpenGatewayProvider:
    """OpenAI-compatible provider for OpenGateway (opengateway.gitlawb.com).

    Default model is Ling 3.0 Flash (free tier), served via the gateway's
    OpenAI-compatible endpoint.
    """

    BASE_URL = "https://opengateway.gitlawb.com/v1"
    DEFAULT_MODEL = "inclusionai/ling-3.0-flash:free"

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
    ) -> None:
        self._api_key = api_key or os.environ.get("OGW_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "OGW_API_KEY is required for OpenGatewayProvider. "
                "Generate a key at https://gitlawb.com/opengateway/dashboard"
            )
        self._model = model or os.environ.get("OGW_MODEL", "") or self.DEFAULT_MODEL

    async def lease(
        self,
        *,
        tools: bool = False,
        structured: bool = False,
        vision: bool = False,
        reasoning: bool = False,
        reasoning_effort: str | None = None,
        priority: str = "balanced",
        excluded_model_ids: frozenset[str] | None = None,
    ) -> ModelSelection:
        from langchain_openai import ChatOpenAI

        llm = _build_llm(
            ChatOpenAI,
            model=self._model,
            base_url=self.BASE_URL,
            api_key=self._api_key,
        )

        logger.info(
            "OpenGatewayProvider: selected %s (tools=%s, vision=%s, reasoning=%s)",
            self._model,
            tools,
            vision,
            reasoning,
        )
        return _selection(
            model_id=self._model,
            provider="opengateway",
            pricing="free",
            quality_hint=0.9,
            llm=llm,
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
        )

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        logger.debug("OpenGatewayProvider: record_failure ignored for %s", model_id)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        logger.debug("OpenGatewayProvider: cooldown ignored for %s", model_id)


# OpenCode Go prefix-cache breakpoint markers. The gateway
# (opencode.ai/zen/go) auto-caches request prefixes with only a ~5 minute TTL;
# ``prompt_cache_key`` + ``prompt_cache_retention`` (set in the provider's
# ``extra_body``) persist that cache across gaps, and ``cache_control``
# breakpoints tell it exactly which points in the conversation to cache so
# earlier turns stay hits as the conversation grows. The pattern -- up to 2
# system messages, the last 2 user/assistant messages, and the last tool
# message -- mirrors the documented opencode-go cache setup; ``ttl: "1h"`` is
# the documented ceiling.
_OPENCODE_CACHE_MARKER: dict[str, Any] = {"type": "ephemeral", "ttl": "1h"}


def _message_has_text(message: dict[str, Any]) -> bool:
    """True when an outbound message carries any non-empty text content."""
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and isinstance(block.get("text"), str)
            and block["text"].strip()
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
                "cache_control": dict(_OPENCODE_CACHE_MARKER),
            }
        ]
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                if not block["text"].strip():
                    continue
                block["cache_control"] = dict(_OPENCODE_CACHE_MARKER)
                return


def _stamp_gateway_cache_breakpoints(payload: dict[str, Any]) -> None:
    """Stamp OpenCode Go prefix-cache breakpoints on an outbound payload.

    Runs at the wire (raw payload dicts), so breakpoints survive serialization
    for every role -- the previous message-level stamping could not reach
    assistant or tool messages. Stale markers from an earlier call are stripped
    first so breakpoints land exactly where this call wants them.
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


class OpenCodeGoProvider:
    """OpenAI-compatible provider for the opencode Zen gateway.

    Serves a single model, DeepSeek V4 Flash, via the gateway's
    ``/zen/go/v1`` chat-completions endpoint. Thinking maps to the gateway's
    DeepSeek-style ``thinking``/``reasoning_effort`` body fields, but it is
    DISABLED by default: on orchestrator-sized prompts the V4 thinking blocks
    consume the entire output budget (measured: 21.5s stream with zero
    content tokens at max_tokens=2048, versus 3.3s and a full answer with
    thinking off — a ~6.5x speedup). Set ``OPENCODEGO_THINKING=1`` to
    re-enable ``thinking``/``reasoning_effort``.

    Prompt caching is enabled provider-wide: a stable ``prompt_cache_key``
    plus ``prompt_cache_retention: "24h"`` (both empirically accepted by the
    gateway for deepseek-v4-flash) keep the DeepSeek prefix cache alive
    across human-wait gaps and longer runs. The ``x-opencode-session`` header
    gives the gateway per-session affinity, mirroring the opencode CLI.
    """

    BASE_URL = "https://opencode.ai/zen/go/v1"
    DEFAULT_MODEL = "deepseek-v4-flash"
    DEFAULT_CACHE_KEY = "z-apply"
    DEFAULT_SESSION_ID = "z-apply"

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        cache_key: str = "",
        session_id: str = "",
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENCODEGO_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "OPENCODEGO_API_KEY is required for OpenCodeGoProvider. "
                "Create a key at https://opencode.ai/"
            )
        self._model = model or os.environ.get("OPENCODEGO_MODEL", "") or self.DEFAULT_MODEL
        self._cache_key = (
            cache_key or os.environ.get("OPENCODEGO_CACHE_KEY", "") or self.DEFAULT_CACHE_KEY
        )
        self._session_id = (
            session_id or os.environ.get("OPENCODEGO_SESSION", "") or self.DEFAULT_SESSION_ID
        )

    async def lease(
        self,
        *,
        tools: bool = False,
        structured: bool = False,
        vision: bool = False,
        reasoning: bool = False,
        reasoning_effort: str | None = None,
        priority: str = "balanced",
        excluded_model_ids: frozenset[str] | None = None,
    ) -> ModelSelection:
        from collections.abc import Sequence

        from langchain_core.language_models import LanguageModelInput
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import Runnable
        from langchain_core.tools import BaseTool
        from langchain_deepseek import ChatDeepSeek

        class ZenGatewayDeepSeek(ChatDeepSeek):
            """DeepSeek V4 reasoner adapted to the opencode Zen gateway.

            DeepSeek V4 models run in thinking mode by default and reject any
            forced ``tool_choice`` ("required", "any", and dict forms) with
            HTTP 400 ``Thinking mode does not support this tool_choice``; only
            "auto" and "none" are accepted. LangChain's structured-output
            strategy forces ``tool_choice="any"`` to pin the schema tool,
            which the gateway rejects. Relax forced tool selection to "auto":
            the schema tool stays bound and the subagent prompt demands it.
            This mirrors the documented DeepSeek V4 integration guidance
            (``supportsToolChoice: false``) and the LiteLLM workaround.
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
                return super().bind_tools(
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
                result = super()._create_chat_result(response, generation_info)
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
                from langchain_core.messages import AIMessage

                payload = super()._get_request_payload(input_, stop=stop, **kwargs)
                messages = payload.get("messages")
                if not isinstance(messages, list):
                    return payload
                source_messages = self._convert_input(input_).to_messages()
                # Rebuild the outbound message list, pairing each tool result
                # to its pending assistant tool call by id, skipping orphan
                # results on the wire only, and pruning assistant tool_calls
                # that never produced a result message. Framework history stays
                # byte-stable so the gateway prefix cache keeps hitting (the
                # middleware that used to delete orphans from history broke the
                # prefix for every later turn). Pruning is required by the
                # gateway contract: an assistant message whose tool_call_ids
                # lack following tool messages is rejected with HTTP 400, which
                # the retry policy does not retry, so every such message used to
                # burn a full model recovery.
                pending_calls: list[dict[str, Any]] = []
                pending_call_ids: set[str] = set()
                pending_assistant: dict[str, Any] | None = None
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
                                # Text-only assistant message: drop the dead
                                # tool_calls, keep the text.
                                pending_assistant.pop("tool_calls", None)
                                pending_assistant.pop("reasoning_content", None)
                            else:
                                # No tool result survived and the assistant
                                # carried no text: the gateway rejects a
                                # message with neither content nor tool_calls,
                                # so drop the empty message entirely.
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
                            message["reasoning_content"] = (
                                reasoning if isinstance(reasoning, str) else ""
                            )
                            pending_assistant = message
                            pending_calls = list(message["tool_calls"])
                            pending_call_ids = set()
                            for pending in pending_calls:
                                pending_id = pending.get("id")
                                if isinstance(pending_id, str) and pending_id:
                                    pending_call_ids.add(pending_id)
                            for call, source_call in zip(
                                pending_calls, source.tool_calls, strict=False
                            ):
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
                            # Orphan tool result: no pending call references it.
                            # Skip it on the wire only; framework history keeps
                            # it so the cached prefix never changes.
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
                _stamp_gateway_cache_breakpoints(payload)
                dump_dir = os.environ.get("Z_APPLY_PAYLOAD_DUMP")
                if dump_dir:
                    import json
                    import time as _time
                    from pathlib import Path

                    Path(dump_dir).mkdir(parents=True, exist_ok=True)
                    Path(dump_dir, f"{_time.time()}.json").write_text(
                        json.dumps(payload, default=str)
                    )
                return payload

        extra_body: dict[str, Any] = {
            "thinking": {"type": "disabled"},
            "prompt_cache_key": self._cache_key,
            "prompt_cache_retention": "24h",
        }
        requested_effort = (reasoning_effort or "").strip().lower()
        if requested_effort in {"low", "medium", "high"}:
            # Explicit per-call reasoning effort turns thinking on for that
            # agent only (e.g. the authentication specialist at "low").
            # Everything else stays in fast non-thinking mode.
            extra_body.update(
                {"thinking": {"type": "enabled"}, "reasoning_effort": requested_effort}
            )
        elif os.environ.get("OPENCODEGO_THINKING", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            extra_body.update({"thinking": {"type": "enabled"}, "reasoning_effort": "high"})

        llm = _build_llm(
            ZenGatewayDeepSeek,
            model=self._model,
            base_url=self.BASE_URL,
            api_key=self._api_key,
            temperature=0.3,
            extra_body=extra_body,
            default_headers={"x-opencode-session": self._session_id},
        )

        logger.info(
            "OpenCodeGoProvider: selected %s (tools=%s, vision=%s, reasoning=%s, "
            "prompt_cache_key=%s, prompt_cache_retention=24h)",
            self._model,
            tools,
            vision,
            reasoning,
            self._cache_key,
        )
        return _selection(
            model_id=self._model,
            provider="opencodego",
            pricing="token",
            quality_hint=0.8,
            llm=llm,
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
        )

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        logger.debug("OpenCodeGoProvider: record_failure ignored for %s", model_id)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        logger.debug("OpenCodeGoProvider: cooldown ignored for %s", model_id)


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    description: str
    env_key: str
    env_attr: str
    default_model: str
    model_env: str
    factory: Callable[[], ModelProvider]


PROVIDERS: dict[str, ProviderSpec] = {}
DETECTION_ORDER: list[str] = []


def register_provider(spec: ProviderSpec) -> None:
    """Register a provider; the registration order sets auto-detect priority."""
    PROVIDERS[spec.name] = spec
    if spec.name not in DETECTION_ORDER:
        DETECTION_ORDER.append(spec.name)


def list_providers() -> list[ProviderSpec]:
    return [PROVIDERS[name] for name in DETECTION_ORDER]


def default_provider_name() -> str:
    """Name of the provider `get_provider` would choose for the current settings."""
    from z_apply_core.config import load_settings

    settings = load_settings()
    requested = (settings.model_provider or "").strip().lower()
    if requested in PROVIDERS:
        return requested
    for name in DETECTION_ORDER:
        spec = PROVIDERS[name]
        if spec.env_attr and getattr(settings, spec.env_attr):
            return name
    return ""


def _make_agnes() -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return AgnesProvider(
        api_key=settings.agnes_api_key,
        model=settings.agnes_model,
        reasoning=settings.agnes_reasoning,
    )


def _make_inferx() -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return InferXProvider(
        api_key=settings.inferx_api_key,
        model=settings.inferx_model,
        reasoning=settings.inferx_reasoning,
        reasoning_effort=settings.inferx_reasoning_effort,
    )


def _make_groq() -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return GroqProvider(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
        reasoning=settings.groq_reasoning,
    )


def _make_opengateway() -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return OpenGatewayProvider(
        api_key=settings.ogw_api_key,
        model=settings.ogw_model,
    )


def _make_opencodego() -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return OpenCodeGoProvider(
        api_key=settings.opencodego_api_key,
        model=settings.opencodego_model,
    )


register_provider(
    ProviderSpec(
        name="opengateway",
        description="OpenGateway (opengateway.gitlawb.com); free Nemotron 3 Ultra by default",
        env_key="OGW_API_KEY",
        env_attr="ogw_api_key",
        default_model=OpenGatewayProvider.DEFAULT_MODEL,
        model_env="OGW_MODEL",
        factory=_make_opengateway,
    )
)
register_provider(
    ProviderSpec(
        name="groq",
        description="Groq hosted endpoints (api.groq.com); Qwen3.6-27B by default",
        env_key="GROQ_API_KEY",
        env_attr="groq_api_key",
        default_model=GroqProvider.DEFAULT_MODEL,
        model_env="GROQ_MODEL",
        factory=_make_groq,
    )
)
register_provider(
    ProviderSpec(
        name="agnes",
        description="Free OpenAI-compatible provider (apihub.agnes-ai.com)",
        env_key="AGNES_API_KEY",
        env_attr="agnes_api_key",
        default_model=AgnesProvider.DEFAULT_MODEL,
        model_env="AGNES_MODEL",
        factory=_make_agnes,
    )
)
register_provider(
    ProviderSpec(
        name="inferx",
        description="OpenAI-compatible InferX endpoints (model.inferx.net)",
        env_key="INFERX_API_KEY",
        env_attr="inferx_api_key",
        default_model=InferXProvider.DEFAULT_MODEL,
        model_env="INFERX_MODEL",
        factory=_make_inferx,
    )
)
register_provider(
    ProviderSpec(
        name="opencodego",
        description="opencode Zen gateway (opencode.ai/zen/go/v1); DeepSeek V4 Flash only",
        env_key="OPENCODEGO_API_KEY",
        env_attr="opencodego_api_key",
        default_model=OpenCodeGoProvider.DEFAULT_MODEL,
        model_env="OPENCODEGO_MODEL",
        factory=_make_opencodego,
    )
)


def provider_from_config(config: Any) -> ModelProvider:
    """Resolve the shared model provider from a runnable config."""
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise ValueError(
            "Run config is missing 'configurable'; cannot locate the shared model provider."
        )
    provider = configurable.get("model_provider")
    if isinstance(provider, ModelProvider):
        return provider
    return get_provider()


def get_provider(
    provider_name: str | None = None,
) -> ModelProvider:
    """Return the configured model provider.

    Resolution:
    1. Explicit `provider_name` if given.
    2. MODEL_PROVIDER env (Settings.model_provider) if it names a registered provider.
    3. Auto-detect: first registered provider whose gating key is set
       (registration order).
    4. Raise with the list of available providers.
    """
    from z_apply_core.config import load_settings

    settings = load_settings()
    requested = (provider_name or settings.model_provider or "").strip().lower()

    if requested:
        spec = PROVIDERS.get(requested)
        if spec is None:
            logger.warning(
                "Unknown model provider %r; available: %s",
                requested,
                ", ".join(PROVIDERS),
            )
        else:
            try:
                return spec.factory()
            except ValueError as exc:
                logger.warning(
                    "Provider %r unavailable: %s; falling back to auto-detection",
                    requested,
                    exc,
                )

    for name in DETECTION_ORDER:
        spec = PROVIDERS[name]
        if not spec.env_attr:
            continue
        if not getattr(settings, spec.env_attr):
            continue
        try:
            return spec.factory()
        except ValueError as exc:
            logger.warning("Provider %r unavailable: %s", name, exc)

    raise ValueError(
        "No model provider configured. Set MODEL_PROVIDER to one of "
        f"{', '.join(PROVIDERS)}, or set a provider API key "
        f"({', '.join(spec.env_key for spec in PROVIDERS.values() if spec.env_key)})."
    )
