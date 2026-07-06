from __future__ import annotations

from z_apply_core.config import Settings, load_settings
from z_apply_core.human.channel import HumanChannel
from z_apply_core.human.telegram import TelegramHumanChannel


def make_configured_human_channel(settings: Settings | None = None) -> HumanChannel | None:
    resolved = settings or load_settings()
    if not resolved.has_telegram:
        return None
    return TelegramHumanChannel(
        token=resolved.telegram_bot_token,
        chat_id=resolved.telegram_group_chat_id,
    )
