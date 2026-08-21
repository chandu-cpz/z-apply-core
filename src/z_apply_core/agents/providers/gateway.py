"""ModelGateway: the one model provider.

Holds the current gateway, model, and runtime thinking override. Builds and
caches chat clients keyed on the effective thinking config, so repeated
``get_model()`` calls with unchanged config return the same object (identity
is how the model router notices a live switch) while a mid-run switch or
reasoning change invalidates the cache.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from z_apply_core.agents.providers.base import GATEWAYS, Gateway
from z_apply_core.agents.providers.opencodego_wire import ZenGatewayChat

logger = logging.getLogger(__name__)

REASONING_MODES: tuple[str, ...] = ("auto", "off", "on")
REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")

# One bounded timeout per model call. Without it every OpenAI-style SDK
# defaults to 600s, so a stalled gateway connection wedges the run silently
# for ten minutes before the retry middleware can recover.
REQUEST_TIMEOUT = 180.0


def _chat_class(key: str) -> type[Any]:
    if key == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek
    if key == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI
    if key == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq
    raise ValueError(f"Unknown chat class {key!r}")


def _zen_chat_class() -> type[Any]:
    from langchain_deepseek import ChatDeepSeek

    class ZenGatewayDeepSeek(ZenGatewayChat, ChatDeepSeek):
        """ChatDeepSeek with the opencode Zen gateway wire adaptations."""

    return ZenGatewayDeepSeek


def _env_flag(name: str | None) -> bool:
    if not name:
        return False
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve(
    gateway: Gateway,
    settings: Any,
    model: str | None,
) -> dict[str, Any]:
    """Read one gateway's live config off settings; raise if the key is missing."""
    api_key = getattr(settings, gateway.settings_attr, "")
    if not api_key:
        raise ValueError(f"{gateway.env_key} is required for the {gateway.name} gateway.")
    return {
        "gateway": gateway,
        "api_key": api_key,
        "model": model or getattr(settings, gateway.model_attr, "") or gateway.default_model,
        "default_thinking": (
            bool(getattr(settings, gateway.reasoning_attr))
            if gateway.reasoning_attr
            else gateway.thinking_default
        ),
        "default_effort": (
            getattr(settings, gateway.effort_attr, "") or None if gateway.effort_attr else None
        ),
    }


class ModelGateway:
    """Current gateway + model + thinking override, with cached clients."""

    def __init__(
        self,
        *,
        gateway: Gateway,
        api_key: str,
        model: str,
        default_thinking: bool,
        default_effort: str | None,
    ) -> None:
        self._gateway = gateway
        self._api_key = api_key
        self._model = model
        self._default_thinking = default_thinking
        self._default_effort = default_effort
        # Runtime reasoning override: "auto" keeps the gateway's configured
        # behavior; "off"/"on" force thinking regardless of role policy. The
        # override survives switches so user intent carries over.
        self._reasoning_mode: str = "on"
        self._reasoning_effort: str | None = "high"
        self._clients: dict[tuple[bool, str | None], BaseChatModel] = {}

    @property
    def name(self) -> str:
        return self._gateway.name

    @property
    def description(self) -> str:
        return self._gateway.description

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def instant_retry(self) -> bool:
        return self._gateway.instant_retry

    @property
    def current_reasoning(self) -> str:
        return self._reasoning_mode

    @property
    def current_reasoning_effort(self) -> str | None:
        return self._reasoning_effort

    def get_model(self, thinking_effort: str | None = None) -> BaseChatModel:
        """Return the chat client for the current config (cached).

        ``thinking_effort`` is the per-role effort request; in auto mode an
        effort the gateway supports turns thinking on for that role only.
        """
        enabled, effort = self._resolve_thinking(thinking_effort)
        client = self._clients.get((enabled, effort))
        if client is None:
            client = self._build_client(enabled, effort)
            self._clients[(enabled, effort)] = client
            logger.info(
                "gateway %s: built client for %s (thinking=%s, effort=%s)",
                self._gateway.name,
                self._model,
                enabled,
                effort,
            )
        return client

    def switch(self, gateway_name: str, model: str | None = None) -> None:
        """Point this gateway at a different backend/model; clears cached clients.

        The reasoning override is deliberately kept so a mid-run switch does
        not silently drop the user's thinking preference.
        """
        gateway = GATEWAYS.get(gateway_name)
        if gateway is None:
            raise ValueError(
                f"Unknown model provider {gateway_name!r}; available: {', '.join(GATEWAYS)}"
            )
        from z_apply_core.config import load_settings

        resolved = _resolve(gateway, load_settings(), model)
        self._gateway = gateway
        self._api_key = resolved["api_key"]
        self._model = resolved["model"]
        self._default_thinking = resolved["default_thinking"]
        self._default_effort = resolved["default_effort"]
        self._clients.clear()

    def set_reasoning(self, reasoning: str, reasoning_effort: str | None = None) -> None:
        """Force thinking on/off (or reset to ``auto``) at runtime."""
        if reasoning not in REASONING_MODES:
            raise ValueError(
                f"unknown reasoning mode {reasoning!r}; expected one of {REASONING_MODES}"
            )
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"unknown reasoning effort {reasoning_effort!r}; expected one of "
                f"{REASONING_EFFORTS} or None"
            )
        self._reasoning_mode = reasoning
        self._reasoning_effort = reasoning_effort
        self._clients.clear()

    def _resolve_thinking(self, thinking_effort: str | None) -> tuple[bool, str | None]:
        if self._reasoning_mode == "off":
            return False, None
        if self._reasoning_mode == "on":
            return True, self._reasoning_effort or self._default_effort
        # auto
        if thinking_effort and thinking_effort in self._gateway.thinking.efforts:
            return True, thinking_effort
        enabled = self._default_thinking or _env_flag(self._gateway.auto_thinking_env)
        return enabled, (self._default_effort if enabled else None)

    def _build_client(self, enabled: bool, effort: str | None) -> BaseChatModel:
        from pydantic import SecretStr

        from z_apply_core.context.model_metrics import attach_first_token_callback

        gateway = self._gateway
        kwargs: dict[str, Any] = {
            "model": self._model,
            "api_key": SecretStr(self._api_key),
            "base_url": gateway.base_url,
            "request_timeout": REQUEST_TIMEOUT,
        }
        if gateway.temperatures is not None:
            kwargs["temperature"] = gateway.temperatures[0] if enabled else gateway.temperatures[1]
        kwargs.update(gateway.thinking.kwargs(model=self._model, enabled=enabled, effort=effort))
        if gateway.wire_fixups:
            # opencode Zen gateway: persist the prefix cache across gaps and
            # give the gateway per-session affinity.
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body["prompt_cache_key"] = (
                os.environ.get("OPENCODEGO_CACHE_KEY", "").strip() or "z-apply"
            )
            extra_body["prompt_cache_retention"] = "24h"
            kwargs["extra_body"] = extra_body
            kwargs["default_headers"] = {
                "x-opencode-session": os.environ.get("OPENCODEGO_SESSION", "").strip() or "z-apply"
            }
        cls = _zen_chat_class() if gateway.wire_fixups else _chat_class(gateway.chat_cls)
        llm: BaseChatModel = cls(**kwargs)
        attach_first_token_callback(llm)
        return llm
