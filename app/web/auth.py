from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from app.config import Settings


class MiniAppAuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(slots=True)
class MiniAppUser:
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None

    @property
    def display_name(self) -> str:
        parts = [self.first_name, self.last_name]
        name = " ".join(part for part in parts if part)
        return name or self.username or str(self.id)


@dataclass(slots=True)
class MiniAppSession:
    user: MiniAppUser
    auth_date: int
    query_id: str | None = None
    start_param: str | None = None


def validate_init_data(
    init_data: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> MiniAppSession:
    if not init_data:
        raise MiniAppAuthError("missing_init_data", "Missing Telegram Mini App init data")
    if not settings.tg_admin_bot_token:
        raise MiniAppAuthError(
            "mini_app_not_configured",
            "Telegram admin bot token is not configured",
        )

    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise MiniAppAuthError("invalid_init_data", "Telegram Mini App init data has no hash")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(
        b"WebAppData",
        settings.tg_admin_bot_token.encode(),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise MiniAppAuthError(
            "invalid_init_data",
            "Telegram Mini App init data signature is invalid",
        )

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise MiniAppAuthError(
            "invalid_init_data",
            "Telegram Mini App auth_date is invalid",
        ) from exc
    if auth_date <= 0:
        raise MiniAppAuthError("invalid_init_data", "Telegram Mini App auth_date is missing")

    current_time = int(time.time()) if now is None else now
    if settings.mini_app_auth_ttl_seconds > 0:
        age = current_time - auth_date
        if age < 0 or age > settings.mini_app_auth_ttl_seconds:
            raise MiniAppAuthError("init_data_expired", "Telegram Mini App init data has expired")

    user = _parse_user(pairs.get("user"))
    if user.id not in settings.admin_telegram_user_ids:
        raise MiniAppAuthError("admin_required", "Telegram user is not allowed to manage this app")

    return MiniAppSession(
        user=user,
        auth_date=auth_date,
        query_id=pairs.get("query_id"),
        start_param=pairs.get("start_param"),
    )


def _parse_user(raw_user: str | None) -> MiniAppUser:
    if not raw_user:
        raise MiniAppAuthError("invalid_init_data", "Telegram Mini App user is missing")
    try:
        payload: Any = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise MiniAppAuthError(
            "invalid_init_data",
            "Telegram Mini App user JSON is invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise MiniAppAuthError("invalid_init_data", "Telegram Mini App user payload is invalid")
    try:
        user_id = int(payload["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MiniAppAuthError("invalid_init_data", "Telegram Mini App user id is invalid") from exc
    return MiniAppUser(
        id=user_id,
        first_name=_optional_string(payload.get("first_name")),
        last_name=_optional_string(payload.get("last_name")),
        username=_optional_string(payload.get("username")),
        language_code=_optional_string(payload.get("language_code")),
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
