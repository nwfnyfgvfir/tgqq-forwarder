from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from app.config import Settings
from app.web.auth import MiniAppAuthError, validate_init_data

TOKEN = "123456:TEST_TOKEN"


def make_init_data(*, user_id: int = 1, auth_date: int = 1_700_000_000) -> str:
    pairs = {
        "auth_date": str(auth_date),
        "query_id": "query-1",
        "user": json.dumps(
            {"id": user_id, "first_name": "Admin", "username": "root"},
            separators=(",", ":"),
        ),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def settings() -> Settings:
    return Settings(
        tg_admin_bot_token=TOKEN,
        admin_telegram_user_ids=[1],
        mini_app_auth_ttl_seconds=3600,
    )


def test_validate_init_data_accepts_admin_user() -> None:
    session = validate_init_data(make_init_data(), settings(), now=1_700_000_100)

    assert session.user.id == 1
    assert session.user.display_name == "Admin"
    assert session.query_id == "query-1"


def test_validate_init_data_rejects_tampered_hash() -> None:
    init_data = make_init_data().replace("Admin", "Attacker")

    with pytest.raises(MiniAppAuthError) as exc_info:
        validate_init_data(init_data, settings(), now=1_700_000_100)

    assert exc_info.value.code == "invalid_init_data"


def test_validate_init_data_rejects_expired_payload() -> None:
    with pytest.raises(MiniAppAuthError) as exc_info:
        validate_init_data(make_init_data(), settings(), now=1_700_010_000)

    assert exc_info.value.code == "init_data_expired"


def test_validate_init_data_rejects_non_admin_user() -> None:
    with pytest.raises(MiniAppAuthError) as exc_info:
        validate_init_data(make_init_data(user_id=2), settings(), now=1_700_000_100)

    assert exc_info.value.code == "admin_required"
