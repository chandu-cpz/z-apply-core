from __future__ import annotations

import logging
import os
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
            model="agnes-2.5-flash",
            base_url="https://apihub.agnes-ai.com/v1",
            api_key=SecretStr(self._api_key),
            temperature=0.3,
        )

        from nim_router.schemas import ModelCapabilities, ModelInfo

        info = ModelInfo(
            id="agnes-2.5-flash",
            capabilities=ModelCapabilities(
                tools=tools,
                structured=structured,
                vision=vision,
                reasoning=reasoning,
            ),
            quality_hint=0.8,
            metadata={"provider": "agnes", "pricing": "free"},
        )

        logger.info("AgnesProvider: selected agnes-2.5-flash (tools=%s, vision=%s)", tools, vision)
        return ModelSelection(info=info, llm=llm, callback=None)

    def record_failure(self, model_id: str, **kwargs: Any) -> None:
        logger.debug("AgnesProvider: record_failure ignored for %s", model_id)

    def cooldown_model(self, model_id: str, seconds: float) -> None:
        logger.debug("AgnesProvider: cooldown ignored for %s", model_id)


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


def get_provider(router: NimRouter | None = None) -> ModelProvider:
    """Return the configured model provider.

    Priority:
    1. If AGNES_API_KEY is set and MODEL_PROVIDER is not "nim", use Agnes.
    2. Otherwise use NIM (requires a router instance).
    """
    from z_apply_core.config import load_settings

    settings = load_settings()
    provider_env = settings.model_provider.lower()
    agnes_key = settings.agnes_api_key

    if provider_env == "agnes" or (provider_env != "nim" and agnes_key):
        try:
            return AgnesProvider(api_key=agnes_key)
        except ValueError as exc:
            logger.warning("Agnes provider unavailable: %s, falling back to NIM", exc)

    if router is None:
        raise ValueError("NIM provider requires a NimRouter instance")
    return NIMProvider(router)
