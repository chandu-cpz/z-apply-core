"""Model provider layer: a table of gateways plus one gateway runtime class.

Public API:
- ``get_model_gateway()`` — resolve the configured gateway (explicit name >
  MODEL_PROVIDER > first gateway whose key is set).
- ``ModelGateway`` — current gateway/model/thinking with cached clients.
- ``GATEWAYS`` — the gateway table (dict order = detection priority).
- ``provider_from_config()`` — pull the shared gateway out of a runnable config.
"""

from __future__ import annotations

import logging
from typing import Any

from z_apply_core.agents.providers.base import GATEWAYS, Gateway
from z_apply_core.agents.providers.gateway import (
    REASONING_EFFORTS,
    REASONING_MODES,
    ModelGateway,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GATEWAYS",
    "REASONING_EFFORTS",
    "REASONING_MODES",
    "Gateway",
    "ModelGateway",
    "default_gateway_name",
    "get_model_gateway",
    "get_provider_catalog",
    "list_gateways",
    "provider_from_config",
]


def _build(gateway: Gateway, settings: Any, model: str | None) -> ModelGateway:
    from z_apply_core.agents.providers.gateway import _resolve

    return ModelGateway(**_resolve(gateway, settings, model))


def get_model_gateway(
    provider_name: str | None = None,
    model: str | None = None,
) -> ModelGateway:
    """Return the configured model gateway.

    Resolution:
    1. Explicit ``provider_name`` if given.
    2. MODEL_PROVIDER (Settings.model_provider) if it names a gateway.
    3. Auto-detect: first gateway (table order) whose API key is set.
    4. Raise with the list of available gateways.
    """
    from z_apply_core.config import load_settings

    settings = load_settings()
    requested = (provider_name or settings.model_provider or "").strip().lower()

    if requested:
        gateway = GATEWAYS.get(requested)
        if gateway is None:
            logger.warning(
                "Unknown model provider %r; available: %s",
                requested,
                ", ".join(GATEWAYS),
            )
        else:
            try:
                return _build(gateway, settings, model)
            except ValueError as exc:
                logger.warning(
                    "Provider %r unavailable: %s; falling back to auto-detection",
                    requested,
                    exc,
                )

    for gateway in GATEWAYS.values():
        if not getattr(settings, gateway.settings_attr, ""):
            continue
        try:
            return _build(gateway, settings, model)
        except ValueError as exc:
            logger.warning("Provider %r unavailable: %s", gateway.name, exc)

    raise ValueError(
        "No model provider configured. Set MODEL_PROVIDER to one of "
        f"{', '.join(GATEWAYS)}, or set a provider API key "
        f"({', '.join(gw.env_key for gw in GATEWAYS.values())})."
    )


def default_gateway_name() -> str:
    """Name of the gateway ``get_model_gateway`` would choose right now."""
    from z_apply_core.config import load_settings

    settings = load_settings()
    requested = (settings.model_provider or "").strip().lower()
    requested_gateway = GATEWAYS.get(requested)
    if requested_gateway is not None and bool(
        getattr(settings, requested_gateway.settings_attr, "")
    ):
        return requested
    for gateway in GATEWAYS.values():
        if getattr(settings, gateway.settings_attr, ""):
            return gateway.name
    return ""


def list_gateways() -> list[Gateway]:
    return list(GATEWAYS.values())


def get_provider_catalog() -> list[dict[str, Any]]:
    """Structured catalog of all gateways with live configuration status."""
    from z_apply_core.config import load_settings

    settings = load_settings()
    active_default = default_gateway_name()
    catalog: list[dict[str, Any]] = []
    for gateway in GATEWAYS.values():
        catalog.append(
            {
                "name": gateway.name,
                "description": gateway.description,
                "default_model": gateway.default_model,
                "suggested_models": list(gateway.suggested_models),
                "env_key": gateway.env_key,
                "configured": bool(getattr(settings, gateway.settings_attr, "")),
                "is_default": gateway.name == active_default,
            }
        )
    return catalog


def provider_from_config(config: Any) -> ModelGateway:
    """Resolve the shared model gateway from a runnable config."""
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise ValueError(
            "Run config is missing 'configurable'; cannot locate the shared model provider."
        )
    provider = configurable.get("model_provider")
    if isinstance(provider, ModelGateway):
        return provider
    return get_model_gateway()
