from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.rules.templates import DEFAULT_MESSAGE_TEMPLATE

# AI 翻译后端默认地址：OpenAI 兼容的 Chat Completions 端点，可换成任意兼容服务。
DEFAULT_RSS_TRANSLATE_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_RSS_TRANSLATE_MODEL = "gpt-4o-mini"


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
    # 可选：Bot API（api.telegram.org）专用代理，形如 http://host:port 或 socks5://host:port。
    # 留空时沿用进程环境变量 HTTP(S)_PROXY；Telethon 走 MTProto，不受此项影响。
    telegram_bot_proxy: str | None = None

    rss_enabled: bool = True
    rss_bot_token: str | None = Field(default=None, exclude=True, repr=False)
    rss_poll_seconds: float = Field(default=10, ge=1, le=3600, allow_inf_nan=False)
    rss_request_timeout_seconds: float = Field(default=20, gt=0, le=300, allow_inf_nan=False)
    rss_max_feed_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    rss_max_entries_per_feed: int = Field(default=500, ge=1, le=10000)
    rss_send_interval_seconds: float = Field(default=1.1, ge=0.01, le=60, allow_inf_nan=False)
    rss_max_send_attempts: int = Field(default=5, ge=1, le=20)
    rss_allowed_private_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # RSS 条目自动翻译：使用 OpenAI 兼容的 /v1/chat/completions 接口。失败自动回退原文。
    rss_translate_enabled: bool = True
    rss_translate_target: str = "ZH"
    # AI 接口地址，可填完整端点（.../v1/chat/completions）或基础地址（.../v1，自动补全）。
    rss_translate_api_url: str = Field(default=DEFAULT_RSS_TRANSLATE_API_URL)
    # 接口凭据；属敏感信息，exclude/repr=False，不进入序列化或 repr。本地无鉴权端点可留空。
    rss_translate_api_key: str | None = Field(default=None, exclude=True, repr=False)
    # 使用的模型名，如 gpt-4o-mini、deepseek-chat、qwen-plus 等。
    rss_translate_model: str = DEFAULT_RSS_TRANSLATE_MODEL
    rss_translate_timeout_seconds: float = Field(
        default=30, gt=0, le=120, allow_inf_nan=False
    )
    rss_translate_max_chars: int = Field(default=1200, ge=100, le=5000)

    @field_validator("telegram_bot_proxy", mode="before")
    @classmethod
    def normalize_bot_proxy(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("TELEGRAM_BOT_PROXY must be a proxy URL string")
        text = value.strip()
        if not text:
            return None
        scheme = text.split("://", 1)[0].lower() if "://" in text else ""
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError(
                "TELEGRAM_BOT_PROXY must start with http://, https://, socks5:// or socks5h://"
            )
        return text

    @field_validator("rss_bot_token", mode="before")
    @classmethod
    def normalize_rss_bot_token(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("rss_translate_target")
    @classmethod
    def validate_rss_translate_target(cls, value: str) -> str:
        text = value.strip()
        if re.fullmatch(r"[A-Za-z]{2,5}(-[A-Za-z]{2,4})?", text) is None:
            raise ValueError("RSS_TRANSLATE_TARGET 仅接受简单语言标签，如 ZH、EN、zh-Hant")
        return text

    @field_validator("rss_translate_api_url", mode="before")
    @classmethod
    def normalize_rss_translate_api_url(cls, value: object) -> object:
        # 空串/空白视为未配置，回退默认地址；None 同理。
        if value is None:
            return DEFAULT_RSS_TRANSLATE_API_URL
        if not isinstance(value, str):
            raise TypeError("RSS_TRANSLATE_API_URL must be a URL string")
        if not value.strip():
            return DEFAULT_RSS_TRANSLATE_API_URL
        return value

    @field_validator("rss_translate_api_url")
    @classmethod
    def validate_rss_translate_api_url(cls, value: str) -> str:
        if len(value) > 2048:
            raise ValueError("RSS_TRANSLATE_API_URL 长度不能超过 2048")
        try:
            parts = urlsplit(value)
            host = parts.hostname
        except ValueError:
            raise ValueError("RSS_TRANSLATE_API_URL 必须是有效的 HTTP(S) URL") from None
        if parts.scheme.lower() not in {"http", "https"} or not host:
            raise ValueError("RSS_TRANSLATE_API_URL 必须以 http:// 或 https:// 开头并包含主机名")
        return value

    @field_validator("rss_translate_api_key", mode="before")
    @classmethod
    def normalize_rss_translate_api_key(cls, value: object) -> object:
        # 空串/空白视为未配置（本地无鉴权端点），返回 None。
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("RSS_TRANSLATE_API_KEY must be a string")
        text = value.strip()
        return text or None

    @field_validator("rss_translate_api_key")
    @classmethod
    def validate_rss_translate_api_key(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 4096:
            raise ValueError("RSS_TRANSLATE_API_KEY 长度不能超过 4096")
        return value

    @field_validator("rss_translate_model", mode="before")
    @classmethod
    def normalize_rss_translate_model(cls, value: object) -> object:
        # 空串/空白回退默认模型。
        if value is None:
            return DEFAULT_RSS_TRANSLATE_MODEL
        if not isinstance(value, str):
            raise TypeError("RSS_TRANSLATE_MODEL must be a string")
        return value.strip() or DEFAULT_RSS_TRANSLATE_MODEL

    @field_validator("rss_translate_model")
    @classmethod
    def validate_rss_translate_model(cls, value: str) -> str:
        if len(value) > 200:
            raise ValueError("RSS_TRANSLATE_MODEL 长度不能超过 200")
        return value

    @field_validator("rss_allowed_private_hosts", mode="before")
    @classmethod
    def parse_rss_allowed_private_hosts(cls, value: object) -> list[str]:
        import ipaddress

        if value is None or value == "":
            return []
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list):
            raise ValueError("RSS 私有主机白名单必须是逗号分隔的主机名")
        hosts = []
        for item in items:
            if not isinstance(item, str):
                raise ValueError("RSS 白名单仅接受明确主机名或 IP")
            host = item.strip().lower().rstrip(".")
            if not host:
                continue
            try:
                host = str(ipaddress.ip_address(host.strip("[]")))
            except ValueError:
                host = host.encode("idna").decode("ascii")
                if any(c in host for c in "/:@*?# ") or not all(
                    part and all(c.isalnum() or c == "-" for c in part)
                    for part in host.split(".")
                ):
                    raise ValueError("RSS 白名单仅接受明确主机名，不接受 URL 或通配符") from None
            if host not in hosts:
                hosts.append(host)
        return hosts

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
    # Hard cap for one QQ outbound send (includes multi-media sequence + retries).
    qq_send_timeout_seconds: float = 120.0

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
