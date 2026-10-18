from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import httpx
import pytest

from app.config import Settings
from app.rules.service import ForwardRuleService
from app.storage.db import Database
from app.web.app import create_mini_app
from app.weixin.models import WeixinContact, WeixinLoginState, WeixinStatus

TOKEN = "123456:TEST_TOKEN"
AUTH_DATE = 1_700_000_000


def make_init_data(*, user_id: int = 1) -> str:
    pairs = {
        "auth_date": str(AUTH_DATE),
        "query_id": "query-1",
        "user": json.dumps({"id": user_id, "first_name": "Admin"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class FakeWeixinService:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.logged_in = False
        self.login = WeixinLoginState(qrcode="qr-1", image_url="https://qr.example/1")
        self.logged_out = False

    def status(self) -> WeixinStatus:
        return WeixinStatus(
            enabled=self.enabled,
            logged_in=self.logged_in,
            connected=self.logged_in,
            account_id="default",
            account_user_id="user-1" if self.logged_in else None,
            base_url="https://ilink.example",
            contacts=1,
            login=self.login,
        )

    def list_contacts(self) -> list[WeixinContact]:
        return [WeixinContact(user_id="wxid_a", name="Alice", has_context_token=True)]

    async def begin_login(self, *, force: bool = False) -> WeixinLoginState:
        return self.login

    def login_status(self) -> WeixinLoginState:
        return self.login

    async def logout(self) -> None:
        self.logged_out = True
        self.logged_in = False


async def build_client(tmp_path, weixin_service) -> tuple[httpx.AsyncClient, Database]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await db.init()
    settings = Settings(
        tg_admin_bot_token=TOKEN,
        admin_telegram_user_ids=[1],
        mini_app_enabled=True,
        mini_app_auth_ttl_seconds=0,
        weixin_enabled=weixin_service is not None,
        weixin_account_id="default",
        telegram_accounts_json='[{"id":"default","session_path":"data/sessions/user.session"}]',
    )
    app = create_mini_app(
        settings=settings,
        service=ForwardRuleService(db),
        account_manager_getter=lambda: None,
        qq_status_getter=lambda: "running",
        qq_targets_getter=lambda: [],
        queue_status_getter=lambda: {"queue_size": 0},
        weixin_service=weixin_service,
    )
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    client.headers.update({"X-Telegram-Init-Data": make_init_data()})
    return client, db


async def test_status_includes_weixin_block(tmp_path) -> None:
    client, db = await build_client(tmp_path, FakeWeixinService())
    async with client:
        response = await client.get("/api/status")
    await db.dispose()

    assert response.status_code == 200
    payload = response.json()
    assert payload["weixin"]["account_id"] == "default"
    assert payload["weixin"]["logged_in"] is False
    assert payload["weixin"]["login"]["status"] == "wait"
    assert payload["weixin"]["login"]["qrcode"] == "qr-1"


async def test_weixin_login_endpoints(tmp_path) -> None:
    service = FakeWeixinService()
    client, db = await build_client(tmp_path, service)
    async with client:
        login_response = await client.post("/api/weixin/login")
        status_response = await client.get("/api/weixin/login")
        contacts_response = await client.get("/api/weixin/contacts")
        logout_response = await client.post("/api/weixin/logout")
    await db.dispose()

    assert login_response.status_code == 200
    assert login_response.json()["qrcode"] == "qr-1"
    assert status_response.status_code == 200
    assert status_response.json()["image_url"] == "https://qr.example/1"
    assert contacts_response.status_code == 200
    assert contacts_response.json()[0]["user_id"] == "wxid_a"
    assert logout_response.json() == {"logged_out": True}
    assert service.logged_out is True


async def test_weixin_routes_return_503_when_disabled(tmp_path) -> None:
    client, db = await build_client(tmp_path, None)
    async with client:
        response = await client.get("/api/weixin/status")
    await db.dispose()

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "weixin_disabled"


async def test_options_exposes_platforms_and_contacts(tmp_path) -> None:
    client, db = await build_client(tmp_path, FakeWeixinService())
    async with client:
        response = await client.get("/api/options")
    await db.dispose()

    payload = response.json()
    assert payload["source_platforms"] == ["telegram", "weixin"]
    assert payload["target_platforms"] == ["qq", "weixin"]
    assert payload["weixin_enabled"] is True
    assert payload["weixin_contacts"][0]["name"] == "Alice"


async def test_create_weixin_target_rule(tmp_path) -> None:
    client, db = await build_client(tmp_path, FakeWeixinService())
    payload = {
        "name": "微信转发",
        "enabled": True,
        "source_platform": "telegram",
        "source_account_id": "default",
        "match_mode": "all",
        "target_platform": "weixin",
        "qq_target_id": "wxid_a",
        "message_template": "{text}",
        "priority": 0,
    }
    async with client:
        response = await client.post("/api/rules", json=payload)
    await db.dispose()

    assert response.status_code == 201, response.text
    rule = response.json()["rule"]
    assert rule["target_platform"] == "weixin"
    assert rule["qq_target_type"] == "friend"
    assert rule["qq_target_id"] == "wxid_a"


@pytest.mark.parametrize("platform", ["weixin"])
async def test_source_account_validation_is_platform_aware(tmp_path, platform) -> None:
    client, db = await build_client(tmp_path, FakeWeixinService())
    payload = {
        "name": "微信来源",
        "source_platform": platform,
        "source_account_id": "default",
        "source_chat_type": "private",
        "qq_target_id": "group-openid",
        "message_template": "{text}",
    }
    async with client:
        response = await client.post("/api/rules", json=payload)
    await db.dispose()

    assert response.status_code == 201, response.text
