from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from nim_router import NimRouter
from nim_router.schemas import ModelSelection

logger = logging.getLogger(__name__)


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
    DEFAULT_MODEL = "agnes-2.5-flash"

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key or os.environ.get("AGNES_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "AGNES_API_KEY is required for AgnesProvider. "
                "Get a key at https://platform.agnes-ai.com/"
            )

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
        from pydantic import SecretStr

        llm = ChatOpenAI(
            model=self.DEFAULT_MODEL,
            base_url=self.BASE_URL,
            api_key=SecretStr(self._api_key),
            temperature=0.3,
        )

        from nim_router.schemas import ModelCapabilities, ModelInfo

        info = ModelInfo(
            id=self.DEFAULT_MODEL,
            capabilities=ModelCapabilities(
                tools=tools,
                structured=structured,
                vision=vision,
                reasoning=reasoning,
            ),
            quality_hint=0.8,
            metadata={"provider": "agnes", "pricing": "free"},
        )

        logger.info("AgnesProvider: selected %s (tools=%s, vision=%s)", self.DEFAULT_MODEL, tools, vision)
        return ModelSelection(info=info, llm=llm, callback=None)

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        logger.debug("AgnesProvider: record_failure ignored for %s", model_id)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        logger.debug("AgnesProvider: cooldown ignored for %s", model_id)


class InferXProvider:
    """OpenAI-compatible provider for InferX hosted endpoints (model.inferx.net)."""

    BASE_URL = "https://model.inferx.net/endpoints/v1"
    DEFAULT_MODEL = "deepseek-v4-flash-0731"

    def __init__(self, api_key: str = "", model: str = "") -> None:
        self._api_key = api_key or os.environ.get("INFERX_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "INFERX_API_KEY is required for InferXProvider. "
                "Create an inference key in the InferX Console (https://model.inferx.net/)."
            )
        self._model = model or os.environ.get("INFERX_MODEL", "") or self.DEFAULT_MODEL

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
        from pydantic import SecretStr

        llm = ChatOpenAI(
            model=self._model,
            base_url=self.BASE_URL,
            api_key=SecretStr(self._api_key),
            temperature=0.3,
        )

        from nim_router.schemas import ModelCapabilities, ModelInfo

        info = ModelInfo(
            id=self._model,
            capabilities=ModelCapabilities(
                tools=tools,
                structured=structured,
                vision=vision,
                reasoning=reasoning,
            ),
            quality_hint=0.8,
            metadata={"provider": "inferx", "pricing": "token"},
        )

        logger.info("InferXProvider: selected %s (tools=%s, vision=%s)", self._model, tools, vision)
        return ModelSelection(info=info, llm=llm, callback=None)

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        logger.debug("InferXProvider: record_failure ignored for %s", model_id)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        logger.debug("InferXProvider: cooldown ignored for %s", model_id)


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
        return await self._router.lease(
            tools=tools,
            structured=structured,
            vision=vision,
            reasoning=reasoning,
            priority=priority,
            **lease_kwargs,
        )

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

    return AgnesProvider(api_key=load_settings().agnes_api_key)


def _make_inferx(_router: NimRouter | None) -> ModelProvider:
    from z_apply_core.config import load_settings

    settings = load_settings()
    return InferXProvider(api_key=settings.inferx_api_key, model=settings.inferx_model)


def _make_nim(router: NimRouter | None) -> ModelProvider:
    if router is None:
        raise ValueError("NIM provider requires a NimRouter instance")
    return NIMProvider(router)


register_provider(
    ProviderSpec(
        name="agnes",
        description="Free OpenAI-compatible provider (apihub.agnes-ai.com)",
        env_key="AGNES_API_KEY",
        env_attr="agnes_api_key",
        default_model=AgnesProvider.DEFAULT_MODEL,
        model_env="",
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
