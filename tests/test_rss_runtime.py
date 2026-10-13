from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

import app.main as main_module
import app.rss.service as rss_service_module
import app.rss.worker as rss_worker_module
from app.config import Settings
from app.rss.fetcher import FeedEntry, FeedResult
from app.rss.publisher import PublishResult
from app.rss.schemas import SubscriptionInput
from app.rss.service import RssService
from app.rss.worker import RssWorker
from app.storage.db import Database
from app.web.server import MiniAppServer

TOKEN = "123456:RUNTIME_ADMIN_TEST_TOKEN"
RSS_TOKEN = "654321:RUNTIME_PUBLISHER_TEST_SECRET"


def make_init_data(*, user_id=1):
    pairs = {
        "auth_date": str(int(datetime.now(UTC).timestamp())),
        "query_id": "runtime-query",
        "user": json.dumps({"id": user_id, "first_name": "测试管理员"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def subscription_payload(**overrides):
    return {
        "name": "Runtime 订阅", "feed_url": "https://example.com/rss",
        "tg_chat_id": "-100123", "initial_policy": "latest", "initial_count": 1,
        **overrides,
    }


class FakeFetcher:
    def __init__(self, settings, events):
        self.settings = settings
        self.events = events
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None
        self.closed = False
        self.result = FeedResult("Runtime Feed", [FeedEntry(
            "runtime-entry", "Runtime 条目", "https://example.com/article", "安全摘要",
            datetime.now(UTC),
        )])

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return self.result

    async def close(self):
        self.events.append("fetcher:close")
        self.closed = True


class FakePublisher:
    configured = True

    def __init__(self, settings, events):
        self.settings = settings
        self.events = events
        self.calls = []
        self.closed = False

    async def send(self, chat_id, text, link=""):
        self.calls.append((chat_id, text, link))
        return PublishResult(len(self.calls), -100123)

    async def stop(self):
        self.events.append("publisher:stop")
        self.closed = True


@dataclass
class RuntimeContext:
    runtime: main_module.ApplicationRuntime
    settings: Settings
    events: list
    logging_calls: list


@pytest.fixture(autouse=True)
def isolate_environment_and_network(monkeypatch):
    fields = {name.lower() for name in Settings.model_fields}
    for name in list(os.environ):
        if name.lower() in fields:
            monkeypatch.delenv(name)

    def forbidden_network(*args, **kwargs):
        pytest.fail("Runtime 测试禁止真实网络访问")

    async def forbidden_http(*args, **kwargs):
        pytest.fail("Runtime 测试禁止真实 HTTP 访问")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_http)


@pytest.fixture
async def runtime_context(tmp_path, monkeypatch):
    events = []
    logging_calls = []

    class StubSettings(Settings):
        def validate_runtime_requirements(self):
            events.append("settings:validate")
            super().validate_runtime_requirements()

    settings = StubSettings(
        _env_file=None,
        data_dir=tmp_path,
        log_dir=tmp_path / "logs",
        media_dir=tmp_path / "media",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}",
        telegram_sessions_dir=tmp_path / "sessions",
        telegram_session_path=tmp_path / "sessions" / "test.session",
        telegram_api_id=12345,
        telegram_api_hash="runtime-test-api-hash",
        qq_bot_appid="runtime-test-appid",
        qq_bot_secret="runtime-test-qq-secret",
        tg_admin_bot_token=TOKEN,
        rss_bot_token=RSS_TOKEN,
        admin_telegram_user_ids=[1],
        mini_app_enabled=True,
        mini_app_host="127.0.0.1",
        mini_app_auth_ttl_seconds=0,
        rss_enabled=True,
        rss_send_interval_seconds=0.01,
    )

    def configure_logging(log_dir, level):
        # 不触碰全局日志 handler，也不创建任何真实日志文件。
        assert log_dir == tmp_path / "logs"
        logging_calls.append((log_dir, level))

    def component(label):
        class StubComponent:
            def __init__(self, *args):
                self.args = args
                self.started = False
                self.status = "running"

            async def start(self):
                events.append(label + ":start")
                self.started = True

            async def stop(self):
                events.append(label + ":stop")
                self.started = False

            async def enqueue(self, *args):
                pytest.fail("RSS 不应进入转发队列")

            def status_snapshot(self):
                return {"queue_size": 0}

            def list_cached_targets(self):
                return []

        return StubComponent

    class StubAccountManager(component("accounts")):
        connected = True

        def is_any_connected(self):
            return self.started and self.connected

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "configure_logging", configure_logging)
    monkeypatch.setattr(main_module, "QQOfficialSender", component("qq"))
    monkeypatch.setattr(main_module, "ForwardQueue", component("queue"))
    monkeypatch.setattr(main_module, "MediaCleanupWorker", component("cleanup"))
    monkeypatch.setattr(main_module, "TelegramAdminBot", component("admin"))
    monkeypatch.setattr(main_module, "TelegramAccountManager", StubAccountManager)
    monkeypatch.setattr(
        rss_service_module, "RssFetcher", lambda config: FakeFetcher(config, events)
    )
    monkeypatch.setattr(
        rss_worker_module, "RssPublisher", lambda config: FakePublisher(config, events)
    )

    runtime = main_module.ApplicationRuntime()
    assert isinstance(runtime.db, Database)
    assert isinstance(runtime.rss_service, RssService)
    assert isinstance(runtime.rss_worker, RssWorker)
    assert isinstance(runtime.mini_app_server, MiniAppServer)
    assert runtime.account_manager is None

    async def idle_scheduler():
        # 隔离自动轮询时序；保留真实 start/stop、手动任务与发送管线。
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.rss_worker, "_scheduler", idle_scheduler)
    real_db_init = runtime.db.init
    real_db_dispose = runtime.db.dispose
    real_rss_start = runtime.rss_worker.start
    real_rss_stop = runtime.rss_worker.stop

    async def init_db():
        events.append("db:init")
        await real_db_init()

    async def dispose_db():
        assert not runtime.rss_worker.status_snapshot()["worker_running"]
        assert runtime.rss_worker._jobs == {}
        events.append("db:dispose")
        await real_db_dispose()

    async def start_rss():
        assert runtime.account_manager.started
        assert runtime.admin_bot.started
        events.append("rss:start")
        await real_rss_start()

    async def stop_rss():
        events.append("rss:stop")
        if runtime.account_manager is not None:
            assert runtime.account_manager.started
        await real_rss_stop()

    async def start_server():
        assert runtime.rss_worker.status_snapshot()["worker_running"]
        events.append("miniapp:start")

    async def stop_server():
        events.append("miniapp:stop")

    monkeypatch.setattr(runtime.db, "init", init_db)
    monkeypatch.setattr(runtime.db, "dispose", dispose_db)
    monkeypatch.setattr(runtime.rss_worker, "start", start_rss)
    monkeypatch.setattr(runtime.rss_worker, "stop", stop_rss)
    # 仅替换服务器监听生命周期，保留真实 MiniAppServer 构造与 server.app。
    monkeypatch.setattr(runtime.mini_app_server, "start", start_server)
    monkeypatch.setattr(runtime.mini_app_server, "stop", stop_server)
    try:
        yield RuntimeContext(runtime, settings, events, logging_calls)
    finally:
        if "db:dispose" not in events:
            await runtime.stop()


async def test_runtime_constructs_real_rss_and_wires_server_app(runtime_context, tmp_path):
    context = runtime_context
    runtime = context.runtime
    assert runtime.settings is context.settings
    assert runtime.rss_service.db is runtime.db is runtime.rule_service.db
    assert runtime.rss_worker.service is runtime.rss_service
    assert runtime.rss_worker.settings is runtime.rss_service.settings is context.settings
    assert isinstance(runtime.rss_service.fetcher, FakeFetcher)
    assert isinstance(runtime.rss_worker.publisher, FakePublisher)
    assert runtime.rss_worker.can_publish.__self__ is runtime
    assert (
        runtime.rss_worker.can_publish.__func__
        is main_module.ApplicationRuntime._can_publish_rss
    )
    assert context.logging_calls == [(tmp_path / "logs", context.settings.log_level)]
    for path in (
        context.settings.data_dir, context.settings.log_dir, context.settings.media_dir,
        context.settings.telegram_sessions_dir, context.settings.telegram_session_path,
        runtime.db.engine.url.database,
    ):
        assert Path(path).is_relative_to(tmp_path)
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "runtime.db").exists()
    assert await runtime.rss_worker.can_publish() is False

    await runtime.start()
    assert (tmp_path / "runtime.db").exists()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.mini_app_server.app),
        base_url="http://test", headers={"X-Telegram-Init-Data": make_init_data()},
    ) as client:
        created = await client.post("/api/rss/subscriptions", json=subscription_payload())
        assert created.status_code == 201, created.text
        sub_id = created.json()["id"]
        stored = await runtime.rss_service.get_subscription(sub_id)
        assert stored["name"] == created.json()["name"]
        direct = await runtime.rss_service.create_subscription(SubscriptionInput(
            **subscription_payload(name="直接创建", enabled=False)
        ))
        listed = await client.get("/api/rss/subscriptions")
        assert {row["id"] for row in listed.json()} == {sub_id, direct["id"]}
        preview = await client.post("/api/rss/preview", json=subscription_payload())
        assert preview.status_code == 200 and preview.json()["feed_title"] == "Runtime Feed"
        assert runtime.rss_worker.publisher.calls == []
        assert len(await runtime.rss_service.list_subscriptions()) == 2
        check = await client.post(f"/api/rss/subscriptions/{sub_id}/check")
        assert check.status_code == 202, check.text
        assert check.json() == {"scheduled": True, "subscription_id": sub_id}
        assert sub_id in runtime.rss_worker._jobs
        await asyncio.wait_for(runtime.rss_worker._jobs[sub_id], timeout=5)
        assert len(runtime.rss_worker.publisher.calls) == 1
        assert len(runtime.rss_service.fetcher.calls) == 2
        status = await client.get("/api/rss/status")
        assert status.status_code == 200
        assert status.json()["worker_running"] is True
        assert status.json()["telegram_ready"] is True
        assert status.json()["publishing_paused"] is False
        assert status.json()["sent_count"] == 1
        history = await client.get(f"/api/rss/subscriptions/{sub_id}/entries")
        assert history.status_code == 200 and history.json()[0]["status"] == "sent"
        for response in (created, listed, preview, status, history):
            assert TOKEN not in response.text and RSS_TOKEN not in response.text


async def test_runtime_start_stop_order_and_dynamic_can_publish(runtime_context):
    context = runtime_context
    runtime = context.runtime
    assert await runtime.rss_worker.can_publish() is False
    await runtime.start()
    assert context.events == [
        "settings:validate", "db:init", "qq:start", "queue:start", "cleanup:start",
        "accounts:start", "admin:start", "rss:start", "miniapp:start",
    ]
    assert runtime.account_manager.args[0] is context.settings
    assert runtime.account_manager.args[1].__self__ is runtime.forward_queue
    assert runtime.admin_bot.args[2]() is runtime.account_manager
    assert await runtime.rss_worker.can_publish() is True
    runtime.account_manager.connected = False
    assert await runtime.rss_worker.can_publish() is False
    runtime.account_manager.connected = True
    await runtime.rule_service.set_paused(True)
    assert await runtime.rss_worker.can_publish() is False
    await runtime.rule_service.set_paused(False)
    assert await runtime.rss_worker.can_publish() is True
    # 回调必须动态读取 manager，不能捕获构造时的 None 或旧对象。
    manager = runtime.account_manager
    runtime.account_manager = None
    assert await runtime.rss_worker.can_publish() is False
    runtime.account_manager = manager
    assert await runtime.rss_worker.can_publish() is True
    await runtime.stop()
    assert context.events[9:] == [
        "miniapp:stop", "rss:stop", "publisher:stop", "fetcher:close", "admin:stop",
        "accounts:stop", "cleanup:stop", "queue:stop", "qq:stop", "db:dispose",
    ]
    assert not runtime.rss_worker.status_snapshot()["worker_running"]
    assert runtime.rss_worker.publisher.closed and runtime.rss_service.fetcher.closed
    assert await runtime.rss_worker.can_publish() is False


@pytest.mark.parametrize("blocker", ["paused", "disconnected", "missing_manager"])
async def test_runtime_real_callback_blocks_and_resumes_pending_publication(
    runtime_context, blocker,
):
    runtime = runtime_context.runtime
    await runtime.start()
    manager = runtime.account_manager
    if blocker == "paused":
        await runtime.rule_service.set_paused(True)
    elif blocker == "disconnected":
        manager.connected = False
    else:
        runtime.account_manager = None
    try:
        assert await runtime.rss_worker.can_publish() is False
        sub = await runtime.rss_service.create_subscription(
            SubscriptionInput(**subscription_payload())
        )
        await runtime.rss_worker.process_subscription(sub["id"])
        assert len(runtime.rss_service.fetcher.calls) == 1
        assert runtime.rss_worker.publisher.calls == []
        pending = (await runtime.rss_service.list_entries(sub["id"]))[0]
        assert pending["status"] == "pending" and pending["attempts"] == 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runtime.mini_app_server.app),
            base_url="http://test", headers={"X-Telegram-Init-Data": make_init_data()},
        ) as client:
            status = await client.get("/api/rss/status")
            assert status.status_code == 200
            assert status.json()["publishing_paused"] is True
            assert status.json()["telegram_ready"] is (blocker == "paused")
            assert status.json()["pending_count"] == 1
            await runtime.rule_service.set_paused(False)
            manager.connected = True
            runtime.account_manager = manager
            assert await runtime.rss_worker.can_publish() is True
            await runtime.rss_worker.process_subscription(sub["id"])
            assert len(runtime.rss_service.fetcher.calls) == 1
            assert len(runtime.rss_worker.publisher.calls) == 1
            status = await client.get("/api/rss/status")
            assert status.json()["publishing_paused"] is False
            assert status.json()["sent_count"] == 1 and status.json()["pending_count"] == 0
    finally:
        runtime.account_manager = manager


async def test_runtime_stop_cancels_pending_api_job_before_disposing_database(runtime_context):
    runtime = runtime_context.runtime
    await runtime.start()
    sub = await runtime.rss_service.create_subscription(SubscriptionInput(**subscription_payload()))
    fetcher = runtime.rss_service.fetcher
    fetcher.release = asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.mini_app_server.app),
        base_url="http://test", headers={"X-Telegram-Init-Data": make_init_data()},
    ) as client:
        response = await asyncio.wait_for(
            client.post(f"/api/rss/subscriptions/{sub['id']}/check"), timeout=5
        )
        assert response.status_code == 202
        await asyncio.wait_for(fetcher.entered.wait(), timeout=5)
        job = runtime.rss_worker._jobs[sub["id"]]
        scheduler = runtime.rss_worker._task
        assert not job.done()
        await asyncio.wait_for(runtime.stop(), timeout=5)
        assert job.cancelled() and scheduler.cancelled()
        assert runtime.rss_worker._jobs == {}
        assert fetcher.closed and runtime.rss_worker.publisher.closed
        assert runtime.rss_worker.publisher.calls == []
        assert runtime_context.events.index("rss:stop") < runtime_context.events.index("db:dispose")
        response = await client.post(f"/api/rss/subscriptions/{sub['id']}/check")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "rss_worker_not_ready"


async def test_runtime_real_scheduler_automatically_processes_subscription(
    runtime_context, monkeypatch,
):
    runtime = runtime_context.runtime
    runtime.settings.rss_poll_seconds = 1
    monkeypatch.setattr(
        runtime.rss_worker, "_scheduler",
        RssWorker._scheduler.__get__(runtime.rss_worker, RssWorker),
    )
    await runtime.start()
    sub = await runtime.rss_service.create_subscription(
        SubscriptionInput(**subscription_payload())
    )
    runtime.rss_worker._wake.set()
    await asyncio.wait_for(runtime.rss_service.fetcher.entered.wait(), timeout=5)
    await asyncio.wait_for(runtime.rss_worker._jobs[sub["id"]], timeout=5)
    assert len(runtime.rss_service.fetcher.calls) == 1
    assert len(runtime.rss_worker.publisher.calls) == 1
    assert (await runtime.rss_service.get_subscription(sub["id"]))["sent_count"] == 1
    task = runtime.rss_worker._task
    await runtime.rss_worker.start()
    assert runtime.rss_worker._task is task


async def test_runtime_global_rss_disabled_rejects_check_without_external_calls(runtime_context):
    runtime = runtime_context.runtime
    runtime.settings.rss_enabled = False
    await runtime.start()
    sub = await runtime.rss_service.create_subscription(SubscriptionInput(**subscription_payload()))
    await runtime.rss_worker.process_subscription(sub["id"], force=True)
    assert runtime.rss_service.fetcher.calls == runtime.rss_worker.publisher.calls == []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.mini_app_server.app),
        base_url="http://test", headers={"X-Telegram-Init-Data": make_init_data()},
    ) as client:
        for action in ("check", "retry"):
            response = await client.post(f"/api/rss/subscriptions/{sub['id']}/{action}")
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "rss_disabled"
        status = await client.get("/api/rss/status")
        assert status.status_code == 200 and status.json()["enabled"] is False
    assert runtime.rss_service.fetcher.calls == runtime.rss_worker.publisher.calls == []
