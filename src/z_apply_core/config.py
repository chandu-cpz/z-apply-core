from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CORE_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    default_username: str = Field(default="", alias="DEFAULT_USERNAME")
    default_password: str = Field(default="", alias="DEFAULT_PASSWORD")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_group_chat_id: str = Field(default="", alias="TELEGRAM_GROUP_CHAT_ID")

    model_config = SettingsConfigDict(
        env_file=CORE_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def has_default_credentials(self) -> bool:
        return bool(self.default_username and self.default_password)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_group_chat_id)


@lru_cache
def load_settings() -> Settings:
    return Settings()
