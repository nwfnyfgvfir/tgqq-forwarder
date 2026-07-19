from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import pytest

from app.config import Settings
from app.qq_official.client import QQTargetInfo
from app.rules.keywords import keywords_from_text_include_regex
from app.rules.service import ForwardRuleService
from app.storage.db import Database
from app.telegram_user.accounts import TelegramAccountStatus
from app.web.app import create_mini_app

TOKEN = "123456:TEST_TOKEN"
AUTH_DATE = 1_700_000_000


@dataclass(slots=True)
class FakeDialog:
    id: int
    name: str
    type: str


class FakeAccountManager:
    def is_any_connected(self) -> bool:
        return True

    def list_status(self) -> list[TelegramAccountStatus]:
        return [
            TelegramAccountStatus(
                id="default",
                enabled=True,
                connected=True,
                authorized=True,
                user_id=10001,
                username="default_user",
                phone=None,
                session_path="data/sessions/user.session",
            ),
            TelegramAccountStatus(
                id="news",
                enabled=True,
                connected=False,
                authorized=False,
                user_id=None,
                username=None,
                phone=None,
                session_path="data/sessions/news.session",
                last_error="not authorized",
            ),
        ]

    def get(self, account_id: str | None = None):
        class _Listener:
            account_id = "default"

        return _Listener()

    async def list_dialogs(
        self,
        *,
        account_id: str | None = None,
        limit: int = 50,
        query: str | None = None,
    ) -> list[FakeDialog]:
        if account_id not in (None, "default", "news"):
            raise KeyError(f"Telegram account not found or not started: {account_id}")
        items = [
            FakeDialog(id=-1001, name="AI Channel", type="channel"),
            FakeDialog(id=-1002, name="Python Group", type="group"),
        ]
        if query:
            lowered = query.lower()
            items = [
                item
                for item in items
                if lowered in item.name.lower() or lowered in str(item.id)
            ]
        return items[:limit]


@pytest.fixture
async def api_client(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await db.init()
    settings = Settings(
        tg_admin_bot_token=TOKEN,
        admin_telegram_user_ids=[1],
        mini_app_enabled=True,
        mini_app_auth_ttl_seconds=0,
        default_message_template="{text}\n{links_note}",
        telegram_accounts_json=(
            '[{"id":"default","session_path":"data/sessions/user.session"},'
            '{"id":"news","session_path":"data/sessions/news.session"}]'
        ),
    )
    service = ForwardRuleService(db)
    app = create_mini_app(
        settings=settings,
        service=service,
        account_manager_getter=lambda: FakeAccountManager(),
        qq_status_getter=lambda: "running",
        qq_targets_getter=lambda: [
            QQTargetInfo(
                target_type="group",
                target_id="group-openid",
                last_message_id="msg-1",
                display_name="测试群",
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
        queue_status_getter=lambda: {
            "queue_size": 3,
            "queue_max_size": 1000,
            "queue_dropped_total": 2,
            "forward_consumer_alive": True,
            "forward_consumer_restarts": 1,
        },
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers.update({"X-Telegram-Init-Data": make_init_data()})
        yield client, service
    await db.dispose()


def make_init_data(*, user_id: int = 1, first_name: str = "Admin") -> str:
    pairs = {
        "auth_date": str(AUTH_DATE),
        "query_id": "query-1",
        "user": json.dumps(
            {"id": user_id, "first_name": first_name, "username": "root"},
            separators=(",", ":"),
        ),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def rule_payload(**overrides):
    data = {
        "name": "AI 转发",
        "enabled": True,
        "source_account_id": "default",
        "source_chat_id": -1001,
        "source_chat_type": "channel",
        "source_sender_id": None,
        "source_sender_is_bot": None,
        "match_mode": "keywords",
        "keywords": ["AI", "Python"],
        "text_include_regex": None,
        "text_exclude_regex": None,
        "media_types": None,
        "qq_target_type": "group",
        "qq_target_id": "group-openid",
        "qq_guild_id": None,
        "qq_channel_id": None,
        "message_template": "{text}\n{links_note}",
        "priority": 0,
    }
    data.update(overrides)
    return data


async def test_status_me_dialogs_targets_and_static(api_client) -> None:
    client, _service = api_client

    me = await client.get("/api/me")
    status = await client.get("/api/status")
    dialogs = await client.get("/api/dialogs", params={"query": "ai", "account": "default"})
    targets = await client.get("/api/qq-targets")
    options = await client.get("/api/options")
    index = await client.get("/")
    app_js = await client.get("/static/app.js?v=20260707-rule-studio-2")

    assert me.status_code == 200
    assert me.json()["user"]["id"] == 1
    assert status.json()["telegram_connected"] is True
    assert status.json()["queue_size"] == 3
    assert status.json()["queue_max_size"] == 1000
    assert status.json()["queue_dropped_total"] == 2
    assert status.json()["forward_consumer_alive"] is True
    assert status.json()["forward_consumer_restarts"] == 1
    assert len(status.json()["telegram_accounts"]) == 2
    assert status.json()["telegram_accounts"][0]["id"] == "default"
    assert dialogs.json()[0]["name"] == "AI Channel"
    assert dialogs.json()[0]["account_id"] == "default"
    assert targets.json()[0]["target_id"] == "group-openid"
    assert "media_path" not in options.json()["template_variables"]
    assert "footer_note" in options.json()["template_variables"]
    assert options.json()["telegram_account_ids"] == ["default", "news"]
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert "TGQQ Forwarder Mini App" in index.text
    assert "/static/styles.css?v=20260707-rule-studio-2" in index.text
    assert "/static/app.js?v=20260707-rule-studio-2" in index.text
    assert app_js.status_code == 200
    assert app_js.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert "./views/rule-studio.js?v=20260707-rule-studio-2" in app_js.text


async def test_rule_crud_preview_and_pause(api_client) -> None:
    client, service = api_client

    created = await client.post("/api/rules", json=rule_payload())
    assert created.status_code == 201
    created_rule = created.json()["rule"]
    assert created_rule["keywords"] == ["AI", "Python"]
    assert created_rule["source_account_id"] == "default"

    duplicate_merge = await client.post(
        "/api/rules",
        json=rule_payload(keywords=["机器人", "AI"]),
    )
    assert duplicate_merge.status_code == 201
    assert duplicate_merge.json()["created"] is False
    rules = await client.get("/api/rules")
    assert len(rules.json()) == 1
    stored_rule = await service.get_rule(created_rule["id"])
    assert stored_rule is not None
    assert keywords_from_text_include_regex(stored_rule.text_include_regex) == [
        "AI",
        "Python",
        "机器人",
    ]

    preview = await client.post(
        "/api/rules/preview",
        json={"rule": rule_payload(), "message": {"text": "Python docs"}},
    )
    assert preview.status_code == 200
    assert preview.json()["matches"] is True
    assert preview.json()["detected_keywords"] == ["Python"]
    assert "***Python***" in preview.json()["rendered_text"]
    assert "检测到关键词：Python" in preview.json()["rendered_text"]

    updated = await client.patch(
        f"/api/rules/{created_rule['id']}",
        json=rule_payload(name="AI 精选", keywords=["AI"]),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "AI 精选"
    assert updated.json()["keywords"] == ["AI"]

    copied = await client.post(
        f"/api/rules/{created_rule['id']}/duplicate",
        json={"enabled": False},
    )
    assert copied.status_code == 200
    assert copied.json()["enabled"] is False

    toggled = await client.patch(
        f"/api/rules/{created_rule['id']}/enabled",
        json={"enabled": False},
    )
    assert toggled.status_code == 200
    assert toggled.json()["enabled"] is False

    paused = await client.patch("/api/settings/paused", json={"paused": True})
    assert paused.status_code == 200
    assert paused.json() == {"paused": True}
    assert await service.is_paused() is True

    deleted = await client.delete(f"/api/rules/{copied.json()['id']}")
    assert deleted.status_code == 200


async def test_auth_required_for_api(api_client) -> None:
    client, _service = api_client
    response = await client.get(
        "/api/me",
        headers={"X-Telegram-Init-Data": make_init_data(user_id=2)},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_required"


async def test_unknown_account_rejected(api_client) -> None:
    client, _service = api_client
    created = await client.post("/api/rules", json=rule_payload(source_account_id="missing"))
    assert created.status_code == 422
