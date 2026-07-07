from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.rules.templates import DEFAULT_MESSAGE_TEMPLATE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "production"
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    log_dir: Path = Path("data/logs")
    media_dir: Path = Path("data/media")
    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session_path: Path = Path("data/sessions/user.session")
    telegram_phone: str | None = None
    telegram_download_media: bool = True
    telegram_forward_link_preview_media: bool = False
    telegram_max_media_mb: int = 20
    telegram_album_buffer_seconds: float = 2.0
    media_cleanup_interval_seconds: int = 3600
    media_retention_seconds: int = 86400

    tg_admin_bot_token: str | None = None
    admin_telegram_user_ids: Annotated[list[int], NoDecode] = []
    tg_admin_bot_connect_timeout: float = 15.0
    tg_admin_bot_request_timeout: float = 30.0
    tg_admin_bot_pool_timeout: float = 15.0
    tg_admin_bot_poll_timeout: int = 30
    tg_admin_bot_poll_read_timeout: float = 45.0

    mini_app_enabled: bool = False
    mini_app_host: str = "0.0.0.0"
    mini_app_port: int = 8000
    mini_app_public_url: str | None = None
    mini_app_auth_ttl_seconds: int = 3600
    mini_app_allowed_origins: Annotated[list[str], NoDecode] = []

    qq_bot_appid: str = ""
    qq_bot_secret: str = ""
    qq_enable_group_c2c: bool = True
    qq_enable_guild_direct_message: bool = False
    qq_allow_send_without_cached_msg_id: bool = True
    qq_use_markdown: bool = True

    forward_queue_size: int = 1000
    default_message_template: str = DEFAULT_MESSAGE_TEMPLATE

    @field_validator("admin_telegram_user_ids", mode="before")
    @classmethod
    def parse_admin_user_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [int(item) for item in value]
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        raise TypeError("ADMIN_TELEGRAM_USER_IDS must be a comma-separated string")

    @field_validator("mini_app_allowed_origins", mode="before")
    @classmethod
    def parse_mini_app_allowed_origins(cls, value: object) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        raise TypeError("MINI_APP_ALLOWED_ORIGINS must be a comma-separated string")

    @field_validator("telegram_api_id")
    @classmethod
    def validate_telegram_api_id(cls, value: int) -> int:
        if value < 0:
            raise ValueError("TELEGRAM_API_ID must be >= 0")
        return value

    def ensure_runtime_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)

    def validate_runtime_requirements(self) -> None:
        missing: list[str] = []
        if not self.telegram_api_id:
            missing.append("TELEGRAM_API_ID")
        if not self.telegram_api_hash:
            missing.append("TELEGRAM_API_HASH")
        if not self.qq_bot_appid:
            missing.append("QQ_BOT_APPID")
        if not self.qq_bot_secret:
            missing.append("QQ_BOT_SECRET")
        if self.mini_app_enabled:
            if not self.tg_admin_bot_token:
                missing.append("TG_ADMIN_BOT_TOKEN")
            if not self.admin_telegram_user_ids:
                missing.append("ADMIN_TELEGRAM_USER_IDS")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required settings: {joined}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_dirs()
    return settings
