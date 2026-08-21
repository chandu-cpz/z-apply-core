from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from z_apply_core.config import load_settings

logger = logging.getLogger(__name__)

_SECRET_TOKEN_RE = re.compile(
    r"<secret>([^<>]+)</secret>"  # legacy format (persisted run history)
    r'|<secret\s+name="([^"]+)"\s+length="\d+"\s*/>'  # enriched format
)
_NEUTRAL_PLACEHOLDER = "[your value here]"


def default_owner_values() -> dict[str, str]:
    """Map the browser-layer redaction names to the real owner identity.

    Only the fixed owner credentials known at build time are returned; empty
    values are filtered out.
    """
    settings = load_settings()
    return {
        name: value
        for name, value in {
            "DEFAULT_USERNAME": settings.default_username,
            "DEFAULT_PASSWORD": settings.default_password,
        }.items()
        if value
    }


def sanitize_human_text(
    text: str,
    *,
    known_values: Mapping[str, str] | None = None,
) -> str:
    """Replace secret-mask tokens before text reaches a human.

    Two token shapes are handled: the legacy ``<secret>NAME</secret>`` and the
    enriched ``<secret name="NAME" length="N"/>`` emitted by the browser layer.
    ``NAME`` is matched case-sensitively against ``known_values`` (the owner
    identity by default). Known names are replaced with their real value so
    the human -- who is the owner -- sees the actual email/password. Unknown
    names are replaced with a neutral placeholder and logged. A literal
    ``<secret`` token is never left in human-facing text, and a known-value
    replacement is never empty.
    """
    if not text:
        return text
    known = default_owner_values() if known_values is None else known_values

    def _replace(match: re.Match[str]) -> str:
        name = " ".join((match.group(1) or match.group(2) or "").strip().split())
        value = known.get(name)
        if value:
            return value
        logger.warning("Masked secret %r reached human text; replaced", name)
        return _NEUTRAL_PLACEHOLDER

    return _SECRET_TOKEN_RE.sub(_replace, text)
