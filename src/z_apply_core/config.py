from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CORE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESUME_PATH = (CORE_ROOT / ".z-apply" / "input" / "Chandrakanth-V-Resume.pdf").resolve()
DEFAULT_SIMPLIFY_ADDON_PATH = Path.home() / "Downloads" / "simplify_jobs-3.0.8"
DEFAULT_CAMOUFOX_BROWSER = ""


class Settings(BaseSettings):
    default_username: str = Field(default="", alias="DEFAULT_USERNAME")
    default_password: str = Field(default="", alias="DEFAULT_PASSWORD")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_group_chat_id: str = Field(default="", alias="TELEGRAM_GROUP_CHAT_ID")
    telegram_proxy: str = Field(default="", alias="TELEGRAM_PROXY")
    telegram_bot_api_base: str = Field(default="", alias="TELEGRAM_BOT_API_BASE")
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
    agnes_model: str = Field(default="agnes-2.0-flash", alias="AGNES_MODEL")
    agnes_reasoning: bool = Field(default=True, alias="AGNES_REASONING")
    inferx_api_key: str = Field(default="", alias="INFERX_API_KEY")
    inferx_model: str = Field(default="deepseek-v4-flash-0731", alias="INFERX_MODEL")
    inferx_reasoning: bool = Field(default=True, alias="INFERX_REASONING")
    inferx_reasoning_effort: str = Field(default="high", alias="INFERX_REASONING_EFFORT")
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_model: str = Field(default="qwen/qwen3.6-27b", alias="GROQ_MODEL")
    groq_reasoning: bool = Field(default=True, alias="GROQ_REASONING")
    ogw_api_key: str = Field(default="", alias="OGW_API_KEY")
    ogw_model: str = Field(
        default="inclusionai/ling-3.0-flash:free",
        alias="OGW_MODEL",
    )
    opencodego_api_key: str = Field(default="", alias="OPENCODEGO_API_KEY")
    opencodego_model: str = Field(default="mimo-v2.5", alias="OPENCODEGO_MODEL")
    orca_api_key: str = Field(default="", alias="ORCA_API_KEY")
    orca_model: str = Field(default="qwen/qwen3.8-27b-free", alias="ORCA_MODEL")
    orca_reasoning: bool = Field(default=True, alias="ORCA_REASONING")
    model_provider: str = Field(default="", alias="MODEL_PROVIDER")
    browser_batch_tools: bool = Field(default=True, alias="BROWSER_BATCH_TOOLS")

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
