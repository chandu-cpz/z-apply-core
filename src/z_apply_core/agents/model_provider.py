from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from nim_router import NimRouter
from nim_router.schemas import ModelSelection

logger = logging.getLogger(__name__)


def _build_llm(
    chat_cls: type[Any],
    *,
    model: str,
    base_url: str,
    api_key: str,
    temperature: float | None = None,
    extra_body: dict[str, Any] | None = None,
    **chat_kwargs: Any,
) -> Any:
    """Construct one OpenAI-compatible chat model with uniform core args.

    All provider chat classes (``ChatOpenAI``, ``ChatDeepSeek``, ``ChatGroq``)
    accept ``model``/``api_key``/``base_url`` under their public pydantic
    aliases, and vendor-specific options ride through ``extra_body`` or the
    class's own typed parameters.
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
    from nim_router.schemas import ModelCapabilities, ModelInfo

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


class OpenCodeGoProvider:
    """OpenAI-compatible provider for the opencode Zen gateway.

    Serves a single model, DeepSeek V4 Flash, via the gateway's
    ``/zen/go/v1`` chat-completions endpoint. Thinking maps to the gateway's
    DeepSeek-style ``thinking``/``reasoning_effort`` body fields.
    """

    BASE_URL = "https://opencode.ai/zen/go/v1"
    DEFAULT_MODEL = "deepseek-v4-flash"

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENCODEGO_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "OPENCODEGO_API_KEY is required for OpenCodeGoProvider. "
                "Create a key at https://opencode.ai/"
            )
        self._model = model or os.environ.get("OPENCODEGO_MODEL", "") or self.DEFAULT_MODEL

    async def lease(
        self,
        *,
        tools: bool = False,
        structured: bool = False,
        vision: bool = False,
        reasoning: bool = False,
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

        extra_body: dict[str, Any] = {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }

        llm = _build_llm(
            ZenGatewayDeepSeek,
            model=self._model,
            base_url=self.BASE_URL,
            api_key=self._api_key,
            temperature=0.3,
            extra_body=extra_body,
        )

        logger.info(
            "OpenCodeGoProvider: selected %s (tools=%s, vision=%s, reasoning=%s)",
            self._model,
            tools,
            vision,
            reasoning,
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


class NIMProvider:
    """Wraps existing NimRouter for NVIDIA NIM models."""

    def __init__(self, router: NimRouter) -> None:
        self._router = router

    async def lease(
        self,
        *,
        tools: bool = False,
        structured: bool = False,
        vision: bool = False,
        reasoning: bool = False,
        priority: str = "balanced",
        excluded_model_ids: frozenset[str] | None = None,
    ) -> ModelSelection:
        lease_kwargs: dict[str, Any] = {}
        if excluded_model_ids:
            lease_kwargs["excluded_model_ids"] = excluded_model_ids
        selection = await self._router.lease(
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
            priority=priority,
            **lease_kwargs,
        )
        from z_apply_core.context.model_metrics import attach_first_token_callback

        attach_first_token_callback(selection.llm)
        return selection

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        self._router.record_failure(model_id, **kwargs)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        self._router.cooldown_model(model_id, seconds)


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    description: str
    env_key: str
    env_attr: str
    default_model: str
    model_env: str
    factory: Callable[[NimRouter | None], ModelProvider]


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


def _make_agnes(_router: NimRouter | None) -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return AgnesProvider(
        api_key=settings.agnes_api_key,
        model=settings.agnes_model,
        reasoning=settings.agnes_reasoning,
    )


def _make_inferx(_router: NimRouter | None) -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return InferXProvider(
        api_key=settings.inferx_api_key,
        model=settings.inferx_model,
        reasoning=settings.inferx_reasoning,
        reasoning_effort=settings.inferx_reasoning_effort,
    )


def _make_groq(_router: NimRouter | None) -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return GroqProvider(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
        reasoning=settings.groq_reasoning,
    )


def _make_opengateway(_router: NimRouter | None) -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return OpenGatewayProvider(
        api_key=settings.ogw_api_key,
        model=settings.ogw_model,
    )


def _make_opencodego(_router: NimRouter | None) -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return OpenCodeGoProvider(
        api_key=settings.opencodego_api_key,
        model=settings.opencodego_model,
    )


def _make_nim(router: NimRouter | None) -> ModelProvider:
    if router is None:
        raise ValueError("NIM provider requires a NimRouter instance")
    return NIMProvider(router)


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
register_provider(
    ProviderSpec(
        name="nim",
        description="NVIDIA NIM models via langchain-nim-router",
        env_key="",
        env_attr="",
        default_model="",
        model_env="",
        factory=_make_nim,
    )
)


def get_provider(
    router: NimRouter | None = None,
    provider_name: str | None = None,
) -> ModelProvider:
    """Return the configured model provider.

    Resolution:
    1. Explicit `provider_name` if given.
    2. MODEL_PROVIDER env (Settings.model_provider) if it names a registered provider.
    3. Auto-detect: first registered provider whose gating key is set
       (registration order; NIM is skipped because it needs a router).
    4. NIM if a router instance was passed.
    5. Raise with the list of available providers.
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
                return spec.factory(router)
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
            return spec.factory(router)
        except ValueError as exc:
            logger.warning("Provider %r unavailable: %s", name, exc)

    if router is not None:
        return NIMProvider(router)

    raise ValueError(
        "No model provider configured. Set MODEL_PROVIDER to one of "
        f"{', '.join(PROVIDERS)}, or set a provider API key "
        f"({', '.join(spec.env_key for spec in PROVIDERS.values() if spec.env_key)})."
    )
