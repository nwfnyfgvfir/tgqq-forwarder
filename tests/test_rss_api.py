from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import select
from telegram.error import BadRequest

from app.config import Settings
from app.rss.errors import RssError
from app.rss.fetcher import FeedEntry, FeedResult
from app.rss.publisher import PublishResult
from app.rss.service import RssService
from app.rss.worker import RssWorker
from app.rules.service import ForwardRuleService
from app.storage.db import Database
from app.storage.models import RssEntry, RssSubscription
from app.web.app import create_mini_app

TOKEN = "123456:RSS_API_TEST_TOKEN"
RSS_TOKEN = "654321:RSS_API_PUBLISHER_SECRET"
PREFIX = "/api/rss"
ENDPOINTS = [
    ("GET", "/status", None),
    ("GET", "/subscriptions", None),
    ("POST", "/subscriptions", "subscription"),
    ("POST", "/preview", "subscription"),
    ("GET", "/subscriptions/1", None),
    ("PATCH", "/subscriptions/1", "subscription"),
    ("PATCH", "/subscriptions/1/enabled", "enabled"),
    ("DELETE", "/subscriptions/1", None),
    ("GET", "/subscriptions/1/entries", None),
    ("POST", "/subscriptions/1/check", None),
    ("POST", "/subscriptions/1/retry", None),
]


def make_init_data(*, user_id=1, token=TOKEN, auth_date=None):
    pairs = {
        "auth_date": str(
            int(datetime.now(UTC).timestamp()) if auth_date is None else auth_date
        ),
        "query_id": "rss-api-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "测试管理员"}, separators=(",", ":")
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def subscription_payload(**overrides):
    return {
        "name": "测试订阅",
        "feed_url": "https://example.com/rss",
        "tg_chat_id": "-100123",
        **overrides,
    }


def feed_entry(number):
    return FeedEntry(
        entry_key=f"entry-{number}",
        title=f"Python 条目 {number}",
        link=f"https://example.com/articles/{number}",
        summary="安全摘要",
        published_at=datetime.now(UTC) + timedelta(seconds=number),
    )


class FakeFetcher:
    def __init__(self):
        self.result = FeedResult("测试 Feed", [feed_entry(i) for i in range(1, 4)])
        self.calls = []
        self.error = None
        self.entered = asyncio.Event()
        self.release = None
        self.closed = False

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self):
        self.closed = True


class FakePublisher:
    configured = True

    def __init__(self):
        self.calls = []
        self.error = None
        self.closed = False

    async def send(self, chat_id, text, link=""):
        self.calls.append((chat_id, text, link))
        if self.error is not None:
            raise self.error
        return PublishResult(len(self.calls), -100123)

    async def stop(self):
        self.closed = True


class FakeAccountManager:
    connected = True

    def is_any_connected(self):
        return self.connected


@dataclass
class ApiContext:
    client: httpx.AsyncClient
    app: object
    db: Database
    settings: Settings
    rules: ForwardRuleService
    service: RssService
    worker: RssWorker
    fetcher: FakeFetcher
    publisher: FakePublisher
    manager: FakeAccountManager
    manager_slot: dict


@pytest.fixture(autouse=True)
def isolate_environment_and_network(monkeypatch):
    fields = {name.lower() for name in Settings.model_fields}
    for name in list(os.environ):
        if name.lower() in fields:
            monkeypatch.delenv(name)

    def forbidden_network(*args, **kwargs):
        pytest.fail("RSS API 测试禁止真实网络访问")

    async def forbidden_http(*args, **kwargs):
        pytest.fail("RSS API 测试禁止真实 HTTP 访问")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_http)


@pytest.fixture
async def api_context(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'rss-api.db'}",
        data_dir=tmp_path,
        log_dir=tmp_path / "logs",
        media_dir=tmp_path / "media",
        telegram_sessions_dir=tmp_path / "sessions",
        telegram_session_path=tmp_path / "sessions" / "test.session",
        tg_admin_bot_token=TOKEN,
        rss_bot_token=RSS_TOKEN,
        admin_telegram_user_ids=[1],
        mini_app_enabled=True,
        mini_app_auth_ttl_seconds=0,
        rss_enabled=True,
        rss_send_interval_seconds=0.01,
    )
    db = Database(settings.database_url)
    fetcher = FakeFetcher()
    publisher = FakePublisher()
    rules = ForwardRuleService(db)
    service = RssService(db, settings, fetcher)
    manager = FakeAccountManager()
    manager_slot = {"manager": manager}

    async def can_publish():
        current = manager_slot["manager"]
        return bool(current and current.is_any_connected() and not await rules.is_paused())

    worker = RssWorker(settings, service, publisher, can_publish=can_publish)

    async def idle_scheduler():
        # 仅停住自动轮询；手动 check/retry 仍使用真实调度、锁和持久化。
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_scheduler", idle_scheduler)
    app = create_mini_app(
        settings=settings,
        service=rules,
        account_manager_getter=lambda: manager_slot["manager"],
        qq_status_getter=lambda: "running",
        qq_targets_getter=lambda: [],
        queue_status_getter=lambda: {},
        rss_service=service,
        rss_worker=worker,
    )
    try:
        await db.init()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.headers["X-Telegram-Init-Data"] = make_init_data()
            yield ApiContext(
                client, app, db, settings, rules, service, worker,
                fetcher, publisher, manager, manager_slot,
            )
    finally:
        await worker.stop()
        await db.dispose()


async def create_subscription(context, **overrides):
    response = await context.client.post(
        f"{PREFIX}/subscriptions", json=subscription_payload(**overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def database_counts(db):
    async with db.session() as session:
        return (
            len((await session.execute(select(RssSubscription))).scalars().all()),
            len((await session.execute(select(RssEntry))).scalars().all()),
        )


def assert_safe_response(response):
    for secret in (TOKEN, RSS_TOKEN, "private-error-secret", "Traceback", "RuntimeError"):
        assert secret not in response.text


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
@pytest.mark.parametrize("auth", ["missing", "bad_signature", "nonadmin"])
async def test_all_endpoints_require_real_admin_auth(api_context, method, path, body, auth):
    context = api_context
    await create_subscription(context)
    await context.worker.start()
    if auth == "missing":
        context.client.headers.pop("X-Telegram-Init-Data")
        expected, code = 401, "missing_init_data"
    elif auth == "bad_signature":
        context.client.headers["X-Telegram-Init-Data"] = make_init_data(token="wrong-token")
        expected, code = 401, "invalid_init_data"
    else:
        context.client.headers["X-Telegram-Init-Data"] = make_init_data(user_id=2)
        expected, code = 403, "admin_required"
    kwargs = {}
    if body is not None:
        kwargs["json"] = subscription_payload() if body == "subscription" else {"enabled": False}
    response = await context.client.request(method, PREFIX + path, **kwargs)
    assert response.status_code == expected, response.text
    assert response.json()["detail"]["code"] == code
    assert_safe_response(response)
    assert await database_counts(context.db) == (1, 0)
    assert (await context.service.get_subscription(1))["enabled"] is True
    assert context.fetcher.calls == context.publisher.calls == []
    assert context.worker._jobs == {}


async def test_expired_hmac_header_is_rejected(api_context):
    api_context.settings.mini_app_auth_ttl_seconds = 60
    auth_date = int((datetime.now(UTC) - timedelta(seconds=120)).timestamp())
    response = await api_context.client.get(
        f"{PREFIX}/status",
        headers={"X-Telegram-Init-Data": make_init_data(auth_date=auth_date)},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "init_data_expired"


async def test_subscription_crud_persists_and_delete_cascades(api_context):
    context = api_context
    created = await create_subscription(context)
    path = f"{PREFIX}/subscriptions/{created['id']}"
    assert created["tg_chat_id"] == "-100123"
    assert created["interval_seconds"] == 600
    assert created["initialized"] is False
    assert created["sent_count"] == created["pending_count"] == created["failed_count"] == 0
    assert (await context.client.get(path)).json() == created
    assert (await context.client.get(f"{PREFIX}/subscriptions")).json() == [created]

    updated = await context.client.patch(
        path,
        json=subscription_payload(
            name="更新订阅", tg_chat_id="@new_channel", interval_seconds=120,
            include_keywords=[" Python ", "Python"], exclude_keywords=["广告"],
        ),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "更新订阅"
    assert updated.json()["include_keywords"] == ["Python"]
    async with context.db.session() as session:
        stored = await session.get(RssSubscription, created["id"])
        assert stored.tg_chat_id == "@new_channel" and stored.interval_seconds == 120
    for enabled in (False, True):
        toggled = await context.client.patch(path + "/enabled", json={"enabled": enabled})
        assert toggled.status_code == 200
        assert toggled.json()["enabled"] is enabled
        assert (await context.service.get_subscription(created["id"]))["enabled"] is enabled

    await context.worker.process_subscription(created["id"])
    assert await database_counts(context.db) == (1, 3)
    deleted = await context.client.delete(path)
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}
    assert await database_counts(context.db) == (0, 0)
    assert (await context.client.get(f"{PREFIX}/subscriptions")).json() == []
    assert (await context.client.get(path)).status_code == 404
    assert context.publisher.calls == []


async def test_preview_renders_filters_without_any_database_or_publish_effect(api_context):
    context = api_context
    existing = await create_subscription(context)
    await context.worker.process_subscription(existing["id"])
    before = await context.service.get_subscription(existing["id"])
    history = await context.service.list_entries(existing["id"])
    calls = len(context.fetcher.calls)
    context.fetcher.result.warnings = ["测试警告", "测试警告"]
    response = await context.client.post(
        f"{PREFIX}/preview",
        json=subscription_payload(
            initial_policy="latest", include_keywords=["Python"],
            exclude_keywords=["条目 2"], message_template="{feed_title}: {title}",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["feed_title"] == "测试 Feed"
    assert body["warnings"] == ["测试警告"]
    assert [row["matches"] for row in body["entries"]] == [True, False, True]
    for row in body["entries"]:
        assert row["rendered_text"].startswith("测试 Feed: Python")
        assert row["link"] in row["rendered_text"]
        assert datetime.fromisoformat(row["published_at"]).tzinfo is not None
    assert context.fetcher.calls[calls:] == [(subscription_payload()["feed_url"], {})]
    assert await database_counts(context.db) == (1, 3)
    assert await context.service.get_subscription(existing["id"]) == before
    assert await context.service.list_entries(existing["id"]) == history
    assert context.publisher.calls == []
    assert_safe_response(response)


async def test_check_returns_202_before_blocked_fetch_then_persists_and_sends(api_context):
    context = api_context
    await context.worker.start()
    created = await create_subscription(context, initial_policy="latest", initial_count=1)
    context.fetcher.release = asyncio.Event()
    path = f"{PREFIX}/subscriptions/{created['id']}"
    response = await asyncio.wait_for(context.client.post(path + "/check"), timeout=5)
    assert response.status_code == 202
    assert response.json() == {"scheduled": True, "subscription_id": created["id"]}
    await asyncio.wait_for(context.fetcher.entered.wait(), timeout=5)
    job = context.worker._jobs[created["id"]]
    assert not job.done()
    assert context.publisher.calls == []
    assert await database_counts(context.db) == (1, 0)
    context.fetcher.release.set()
    await asyncio.wait_for(job, timeout=5)
    fetched = await context.client.get(path)
    assert fetched.json()["initialized"] is True
    assert fetched.json()["sent_count"] == 1
    assert len(context.publisher.calls) == 1
    assert context.publisher.calls[0][2].endswith("/3")


async def test_retry_returns_202_and_only_resets_failed_entries(api_context):
    context = api_context
    created = await create_subscription(context, initial_policy="latest", initial_count=1)
    context.publisher.error = BadRequest("private-error-secret")
    await context.worker.process_subscription(created["id"])
    failed = (await context.service.list_entries(created["id"]))[0]
    assert failed["status"] == "failed" and failed["attempts"] == 1
    skipped_ids = {
        row["id"] for row in await context.service.list_entries(created["id"])
        if row["status"] == "skipped"
    }
    context.publisher.error = None
    await context.worker.start()
    response = await context.client.post(f"{PREFIX}/subscriptions/{created['id']}/retry")
    assert response.status_code == 202
    assert response.json() == {
        "scheduled": True, "subscription_id": created["id"], "retried_count": 1,
    }
    await asyncio.wait_for(context.worker._jobs[created["id"]], timeout=5)
    rows = await context.service.list_entries(created["id"])
    sent = next(row for row in rows if row["id"] == failed["id"])
    assert sent["status"] == "sent" and sent["attempts"] == 1
    assert sent["last_error"] is None and sent["next_retry_at"] is None
    assert {row["id"] for row in rows if row["status"] == "skipped"} == skipped_ids
    assert len(context.publisher.calls) == 2
    assert len(context.fetcher.calls) == 1


@pytest.mark.parametrize("action", ["check", "retry"])
@pytest.mark.parametrize("disabled", ["subscription", "global"])
async def test_disabled_check_and_retry_return_409(api_context, action, disabled):
    context = api_context
    created = await create_subscription(context, enabled=disabled != "subscription")
    if disabled == "global":
        context.settings.rss_enabled = False
    await context.worker.start()
    response = await context.client.post(f"{PREFIX}/subscriptions/{created['id']}/{action}")
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "rss_disabled", "message": "RSS 订阅或功能已禁用",
    }
    assert context.fetcher.calls == context.publisher.calls == []
    assert context.worker._jobs == {}
    assert_safe_response(response)


@pytest.mark.parametrize("worker_state", ["missing", "not_started", "stopped"])
@pytest.mark.parametrize("action", ["check", "retry"])
async def test_missing_or_nonrunning_worker_returns_503(api_context, worker_state, action):
    context = api_context
    created = await create_subscription(context)
    if worker_state == "stopped":
        await context.worker.start()
        await context.worker.stop()
    app = context.app
    if worker_state == "missing":
        app = create_mini_app(
            settings=context.settings, service=context.rules,
            account_manager_getter=lambda: context.manager,
            qq_status_getter=lambda: "running", qq_targets_getter=lambda: [],
            queue_status_getter=lambda: {}, rss_service=context.service,
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"X-Telegram-Init-Data": make_init_data()},
    ) as client:
        response = await client.post(f"{PREFIX}/subscriptions/{created['id']}/{action}")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "rss_worker_not_ready"
        status = await client.get(f"{PREFIX}/status")
        assert status.status_code == 200 and status.json()["worker_running"] is False
        assert (await client.get(f"{PREFIX}/subscriptions")).json()[0]["id"] == created["id"]
    assert context.fetcher.calls == context.publisher.calls == []


async def test_optional_service_and_worker_support_crud_on_shared_database(api_context):
    context = api_context
    app = create_mini_app(
        settings=context.settings, service=context.rules,
        account_manager_getter=lambda: None, qq_status_getter=lambda: "running",
        qq_targets_getter=lambda: [], queue_status_getter=lambda: {},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"X-Telegram-Init-Data": make_init_data()},
    ) as client:
        created = await client.post(f"{PREFIX}/subscriptions", json=subscription_payload())
        assert created.status_code == 201
        stored = await context.service.get_subscription(created.json()["id"])
        assert stored["name"] == created.json()["name"]
        assert (await client.get(f"{PREFIX}/status")).json()["worker_running"] is False
        assert (await client.post(
            f"{PREFIX}/subscriptions/{stored['id']}/check"
        )).status_code == 503


INVALID_PAYLOADS = [
    ("name", ""),
    ("feed_url", "ftp://example.com/rss"),
    ("feed_url", "https://user:password@example.com/rss"),
    ("feed_url", "https://example.com/rss#fragment"),
    ("tg_chat_id", -100123),
    ("tg_chat_id", True),
    ("tg_chat_id", None),
    ("tg_chat_id", "123"),
    ("tg_chat_id", "@x"),
    ("interval_seconds", 59),
    ("interval_seconds", 86401),
    ("initial_policy", "all"),
    ("initial_count", 0),
    ("initial_count", 21),
    ("max_summary_length", 99),
    ("message_template", "{title.__class__}"),
    ("unexpected", "field"),
]


@pytest.mark.parametrize("method,path", [
    ("POST", "/subscriptions"), ("PATCH", "/subscriptions/1"), ("POST", "/preview"),
])
@pytest.mark.parametrize("field,value", INVALID_PAYLOADS)
async def test_payload_validation_returns_422_without_side_effects(
    api_context, method, path, field, value,
):
    context = api_context
    await create_subscription(context)
    before = await context.service.get_subscription(1)
    response = await context.client.request(
        method, PREFIX + path, json=subscription_payload(**{field: value})
    )
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert any(row["loc"] == ["body", field] for row in errors)
    if field == "tg_chat_id" and not isinstance(value, str):
        assert any(row["type"] == "string_type" for row in errors)
    assert await database_counts(context.db) == (1, 0)
    assert await context.service.get_subscription(1) == before
    assert context.fetcher.calls == context.publisher.calls == []


@pytest.mark.parametrize("body", [{}, {"enabled": []}, {"enabled": True, "unknown": 1}])
async def test_enabled_body_validation_returns_422(api_context, body):
    created = await create_subscription(api_context)
    response = await api_context.client.patch(
        f"{PREFIX}/subscriptions/{created['id']}/enabled", json=body
    )
    assert response.status_code == 422
    assert (await api_context.service.get_subscription(created["id"]))["enabled"] is True


@pytest.mark.parametrize("method,path,body", ENDPOINTS[4:])
async def test_unknown_subscription_returns_safe_404(api_context, method, path, body):
    context = api_context
    await context.worker.start()
    kwargs = {}
    if body is not None:
        kwargs["json"] = subscription_payload() if body == "subscription" else {"enabled": False}
    response = await context.client.request(method, PREFIX + path, **kwargs)
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "rss_not_found", "message": "RSS 订阅不存在"}}
    assert_safe_response(response)
    assert context.fetcher.calls == context.publisher.calls == []


@pytest.mark.parametrize("error_kind", ["unexpected", "domain"])
async def test_preview_errors_have_safe_structured_response(api_context, error_kind):
    context = api_context
    if error_kind == "unexpected":
        context.fetcher.error = RuntimeError(f"private-error-secret {RSS_TOKEN} {TOKEN}")
        code, status, message = "rss_fetch_failed", 502, "订阅预览抓取失败"
    else:
        context.fetcher.error = RssError("rss_private_host", 422, "订阅地址禁止访问非公网主机")
        code, status, message = "rss_private_host", 422, "订阅地址禁止访问非公网主机"
    response = await context.client.post(f"{PREFIX}/preview", json=subscription_payload())
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code, "message": message}}
    assert_safe_response(response)
    assert await database_counts(context.db) == (0, 0)
    assert context.publisher.calls == []


@pytest.mark.parametrize("failure", ["fetch", "publish"])
async def test_background_errors_are_sanitized_in_subscription_history_and_status(
    api_context, failure,
):
    context = api_context
    created = await create_subscription(context, initial_policy="latest", initial_count=1)
    secret_error = RuntimeError(f"private-error-secret {TOKEN} {RSS_TOKEN}")
    if failure == "fetch":
        context.fetcher.error = secret_error
    else:
        context.publisher.error = secret_error
    await context.worker.start()
    path = f"{PREFIX}/subscriptions/{created['id']}"
    check = await context.client.post(path + "/check")
    assert check.status_code == 202
    await asyncio.wait_for(context.worker._jobs[created["id"]], timeout=5)
    subscription = await context.client.get(path)
    history = await context.client.get(path + "/entries")
    status = await context.client.get(f"{PREFIX}/status")
    for response in (subscription, history, status):
        assert response.status_code == 200
        assert_safe_response(response)
    if failure == "fetch":
        assert subscription.json()["last_error"] == "RSS 抓取失败"
        assert subscription.json()["failure_count"] == 1
        assert history.json() == []
    else:
        assert subscription.json()["failed_count"] == 1
        failed = next(row for row in history.json() if row["status"] == "failed")
        assert failed["last_error"] == "TG 发送失败或响应未知，重试可能重复"
        assert failed["attempts"] == 1 and failed["next_retry_at"] is None
        assert status.json()["failed_count"] == 1


async def test_entry_history_is_scoped_descending_and_limit_is_bounded(api_context):
    context = api_context
    first = await create_subscription(context)
    second = await create_subscription(context, name="另一订阅")
    context.fetcher.result = FeedResult("历史 Feed", [feed_entry(i) for i in range(105)])
    await context.worker.process_subscription(first["id"])
    await context.worker.process_subscription(second["id"])
    path = f"{PREFIX}/subscriptions/{first['id']}/entries"
    for limit, expected in ((None, 50), (1, 1), (2, 2), (100, 100)):
        response = await context.client.get(path, params={} if limit is None else {"limit": limit})
        assert response.status_code == 200
        rows = response.json()
        assert len(rows) == expected
        assert [row["id"] for row in rows] == sorted([row["id"] for row in rows], reverse=True)
        assert {row["subscription_id"] for row in rows} == {first["id"]}
        assert rows[0]["title"] == "Python 条目 104"
        assert {row["status"] for row in rows} == {"skipped"}
        assert datetime.fromisoformat(rows[0]["created_at"]).tzinfo is not None
        assert_safe_response(response)
    assert context.publisher.calls == []
    assert await database_counts(context.db) == (2, 210)


async def test_unexpected_job_error_is_sanitized_in_worker_status(api_context, monkeypatch):
    context = api_context
    created = await create_subscription(context)

    async def broken_process(*args, **kwargs):
        raise RuntimeError(f"private-error-secret {TOKEN} {RSS_TOKEN}")

    monkeypatch.setattr(context.worker, "process_subscription", broken_process)
    await context.worker.start()
    response = await context.client.post(f"{PREFIX}/subscriptions/{created['id']}/check")
    assert response.status_code == 202
    await asyncio.wait_for(context.worker._jobs[created["id"]], timeout=5)
    status = await context.client.get(f"{PREFIX}/status")
    assert status.status_code == 200
    assert status.json()["worker_running"] is True
    assert status.json()["last_worker_error"] == "RSS 订阅处理失败"
    assert_safe_response(status)
    assert context.fetcher.calls == context.publisher.calls == []


@pytest.mark.parametrize("limit", [0, -1, 101, "abc", "1.5"])
async def test_invalid_entry_history_limit_returns_422(api_context, limit):
    created = await create_subscription(api_context)
    response = await api_context.client.get(
        f"{PREFIX}/subscriptions/{created['id']}/entries", params={"limit": limit}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


async def test_status_fields_counts_and_subscription_state_never_expose_tokens(api_context):
    context = api_context
    first = await create_subscription(context)
    await create_subscription(context, enabled=False)
    async with context.db.session() as session:
        for number, state in enumerate(("pending", "sending", "failed", "sent", "skipped")):
            session.add(RssEntry(
                subscription_id=first["id"], entry_key=f"state-{number}",
                title="状态测试", link="https://example.com/article", summary="摘要", status=state,
            ))
    await context.worker.start()
    response = await context.client.get(f"{PREFIX}/status")
    assert response.status_code == 200
    # start 会把 sending 恢复为 pending，两者统计应保持不变。
    assert response.json() == {
        "enabled": True, "publisher_configured": True,
        "total_subscriptions": 2, "enabled_subscriptions": 1,
        "pending_count": 2, "failed_count": 1, "sent_count": 1,
        "worker_running": True, "last_worker_error": None,
        "telegram_ready": True, "publishing_paused": False,
    }
    subscription = await context.client.get(f"{PREFIX}/subscriptions/{first['id']}")
    state = subscription.json()
    assert state["pending_count"] == 2 and state["failed_count"] == state["sent_count"] == 1
    assert {
        "initialized", "feed_version", "etag", "last_modified", "last_checked_at",
        "last_success_at", "last_error", "failure_count", "next_check_at", "feed_title",
        "created_at", "updated_at",
    } <= state.keys()
    assert state["initialized"] is False and state["failure_count"] == 0
    assert state["last_checked_at"] is state["last_success_at"] is state["next_check_at"] is None
    for item in (response, subscription, await context.client.get(f"{PREFIX}/subscriptions")):
        assert_safe_response(item)
        assert "bot_token" not in item.text


@pytest.mark.parametrize("manager_state,paused,ready,publishing_paused", [
    ("connected", False, True, False),
    ("connected", True, True, True),
    ("disconnected", False, False, True),
    ("disconnected", True, False, True),
    ("missing", False, False, True),
])
async def test_status_reflects_pause_and_telegram_readiness(
    api_context, manager_state, paused, ready, publishing_paused,
):
    context = api_context
    context.manager.connected = manager_state == "connected"
    if manager_state == "missing":
        context.manager_slot["manager"] = None
    await context.rules.set_paused(paused)
    response = await context.client.get(f"{PREFIX}/status")
    assert response.status_code == 200
    assert response.json()["telegram_ready"] is ready
    assert response.json()["publishing_paused"] is publishing_paused
    assert await context.worker.can_publish() is (not publishing_paused)


@pytest.mark.parametrize("blocker", ["pause", "telegram"])
async def test_pause_or_tg_disconnect_keeps_pending_until_ready(api_context, blocker):
    context = api_context
    created = await create_subscription(context, initial_policy="latest", initial_count=1)
    if blocker == "pause":
        paused = await context.client.patch("/api/settings/paused", json={"paused": True})
        assert paused.status_code == 200
    else:
        context.manager.connected = False
    await context.worker.process_subscription(created["id"])
    assert len(context.fetcher.calls) == 1
    assert context.publisher.calls == []
    state = await context.service.get_subscription(created["id"])
    assert state["pending_count"] == 1
    pending = next(row for row in await context.service.list_entries(created["id"])
                   if row["status"] == "pending")
    assert pending["attempts"] == 0
    assert (await context.client.get(f"{PREFIX}/status")).json()["publishing_paused"] is True
    await context.rules.set_paused(False)
    context.manager.connected = True
    await context.worker.process_subscription(created["id"])
    assert len(context.fetcher.calls) == 1 and len(context.publisher.calls) == 1
    assert (await context.service.get_subscription(created["id"]))["sent_count"] == 1
