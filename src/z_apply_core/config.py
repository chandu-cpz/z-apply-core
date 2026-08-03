from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CORE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIMPLIFY_ADDON_PATH = Path.home() / "Downloads" / "simplify_jobs-2.3.0"
DEFAULT_CAMOUFOX_BROWSER = "official/150.0.2-alpha.26"


class Settings(BaseSettings):
    default_username: str = Field(default="", alias="DEFAULT_USERNAME")
    default_password: str = Field(default="", alias="DEFAULT_PASSWORD")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_group_chat_id: str = Field(default="", alias="TELEGRAM_GROUP_CHAT_ID")
    gmail_credentials_path: Path = Field(
        default=CORE_ROOT / "credentials.json",
        alias="GMAIL_CREDENTIALS_PATH",
    )
    gmail_token_path: Path = Field(
        default=CORE_ROOT / "token.json",
        alias="GMAIL_TOKEN_PATH",
    )
    camoufox_browser: str = Field(
        default=DEFAULT_CAMOUFOX_BROWSER,
        alias="CAMOUFOX_BROWSER",
    )
    simplify_addon_path: Path = Field(
        default=DEFAULT_SIMPLIFY_ADDON_PATH,
        alias="SIMPLIFY_ADDON_PATH",
    )
    agnes_api_key: str = Field(default="", alias="AGNES_API_KEY")
    inferx_api_key: str = Field(default="", alias="INFERX_API_KEY")
    inferx_model: str = Field(default="deepseek-v4-flash-0731", alias="INFERX_MODEL")
    inferx_reasoning: bool = Field(default=True, alias="INFERX_REASONING")
    inferx_reasoning_effort: str = Field(default="high", alias="INFERX_REASONING_EFFORT")
    model_provider: str = Field(default="", alias="MODEL_PROVIDER")

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

    @property
    def has_gmail(self) -> bool:
        return self.gmail_credentials_path.is_file() and self.gmail_token_path.is_file()


@lru_cache
def load_settings() -> Settings:
    return Settings()
