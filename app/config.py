from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.rules.templates import DEFAULT_MESSAGE_TEMPLATE


@dataclass(slots=True)
class TelegramAccountConfig:
    id: str
    session_path: Path
    phone: str | None = None
    enabled: bool = True
    api_id: int | None = None
    api_hash: str | None = None

    def resolved_api_id(self, fallback: int) -> int:
        return int(self.api_id or fallback)

    def resolved_api_hash(self, fallback: str) -> str:
        return str(self.api_hash or fallback)


def _default_account_from_legacy(
    *,
    session_path: Path,
    phone: str | None,
    api_id: int,
    api_hash: str,
) -> list[TelegramAccountConfig]:
    return [
        TelegramAccountConfig(
            id="default",
            session_path=session_path,
            phone=phone,
            enabled=True,
            api_id=api_id or None,
            api_hash=api_hash or None,
        )
    ]


def default_account_session_path(sessions_dir: Path, account_id: str) -> Path:
    """Per-account session layout: sessions/<id>/account.session."""
    return Path(sessions_dir) / account_id / "account.session"


def _normalize_session_path(raw: object, *, sessions_dir: Path, account_id: str) -> Path:
    """Resolve an account session path.

    - omitted/empty → sessions/<id>/account.session
    - directory path → <dir>/account.session
    - file path → used as-is (explicit override / legacy flat files)
    """
    if raw in (None, ""):
        return default_account_session_path(sessions_dir, account_id)

    path = Path(str(raw))
    # Allow writing session_path as a directory for an account.
    if path.suffix == "" or path.name in {account_id, "session", "sessions"}:
        return path / "account.session"
    if path.exists() and path.is_dir():
        return path / "account.session"
    return path


def _parse_accounts_payload(
    raw: object,
    *,
    sessions_dir: Path,
    default_session_path: Path,
    default_phone: str | None,
    default_api_id: int,
    default_api_hash: str,
) -> list[TelegramAccountConfig]:
    if raw is None or raw == "" or raw == []:
        return _default_account_from_legacy(
            session_path=default_session_path,
            phone=default_phone,
            api_id=default_api_id,
            api_hash=default_api_hash,
        )

    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return _default_account_from_legacy(
                session_path=default_session_path,
                phone=default_phone,
                api_id=default_api_id,
                api_hash=default_api_hash,
            )
        data = json.loads(text)

    if isinstance(data, dict):
        items: list[dict[str, Any]] = []
        for key, value in data.items():
            if not isinstance(value, dict):
                raise TypeError("TELEGRAM_ACCOUNTS_JSON object values must be objects")
            item = dict(value)
            item.setdefault("id", key)
            items.append(item)
        data = items

    if not isinstance(data, list):
        raise TypeError("TELEGRAM_ACCOUNTS_JSON must be a JSON array or object")

    accounts: list[TelegramAccountConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise TypeError(f"TELEGRAM_ACCOUNTS_JSON[{index}] must be an object")
        account_id = str(item.get("id") or f"account{index + 1}").strip()
        if not account_id:
            raise ValueError(f"TELEGRAM_ACCOUNTS_JSON[{index}].id must not be empty")
        if account_id in seen:
            raise ValueError(f"Duplicate Telegram account id: {account_id}")
        seen.add(account_id)

        session_raw = item.get("session_path") or item.get("session")
        session_path = _normalize_session_path(
            session_raw,
            sessions_dir=sessions_dir,
            account_id=account_id,
        )

        phone = item.get("phone", default_phone if account_id == "default" else None)
        if phone is not None:
            phone = str(phone).strip() or None

        enabled = item.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}

        api_id = item.get("api_id")
        api_hash = item.get("api_hash")
        accounts.append(
            TelegramAccountConfig(
                id=account_id,
                session_path=session_path,
                phone=phone,
                enabled=bool(enabled),
                api_id=int(api_id) if api_id not in (None, "") else None,
                api_hash=str(api_hash).strip() if api_hash not in (None, "") else None,
            )
        )

    if not accounts:
        return _default_account_from_legacy(
            session_path=default_session_path,
            phone=default_phone,
            api_id=default_api_id,
            api_hash=default_api_hash,
        )
    return accounts


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
    # Root directory for multi-account sessions: <dir>/<account_id>/account.session
    telegram_sessions_dir: Path = Path("data/sessions")
    # Legacy single-account session file. Used only when TELEGRAM_ACCOUNTS_JSON is unset.
    telegram_session_path: Path = Path("data/sessions/user.session")
    telegram_phone: str | None = None
    # JSON array/object describing one or more Telegram user accounts.
    # Empty/unset falls back to TELEGRAM_SESSION_PATH + TELEGRAM_PHONE as account "default".
    # When session_path is omitted, path becomes TELEGRAM_SESSIONS_DIR/<id>/account.session
    telegram_accounts_json: Annotated[str | None, NoDecode] = None
    telegram_accounts: list[TelegramAccountConfig] = Field(default_factory=list, exclude=True)
    telegram_require_all_accounts: bool = True
    telegram_reconnect_enabled: bool = True
    telegram_reconnect_delay_seconds: float = 5.0
    # When true, messages with the same (chat_id, message_id) from multiple accounts
    # are only processed once (first account wins).
    telegram_dedupe_cross_account: bool = False
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

    @model_validator(mode="after")
    def build_telegram_accounts(self) -> Settings:
        self.telegram_accounts = _parse_accounts_payload(
            self.telegram_accounts_json,
            sessions_dir=self.telegram_sessions_dir,
            default_session_path=self.telegram_session_path,
            default_phone=self.telegram_phone,
            default_api_id=self.telegram_api_id,
            default_api_hash=self.telegram_api_hash,
        )
        return self

    def get_telegram_account(self, account_id: str) -> TelegramAccountConfig | None:
        for account in self.telegram_accounts:
            if account.id == account_id:
                return account
        return None

    def enabled_telegram_accounts(self) -> list[TelegramAccountConfig]:
        return [account for account in self.telegram_accounts if account.enabled]

    def account_session_path(self, account_id: str) -> Path:
        account = self.get_telegram_account(account_id)
        if account is not None:
            return account.session_path
        return default_account_session_path(self.telegram_sessions_dir, account_id)

    def ensure_runtime_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.telegram_sessions_dir.mkdir(parents=True, exist_ok=True)
        for account in self.telegram_accounts:
            account.session_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep legacy path parent for older docs/tools.
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
        if not self.enabled_telegram_accounts():
            raise RuntimeError("No enabled Telegram accounts configured")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_dirs()
    return settings
