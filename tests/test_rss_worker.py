from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from app.config import Settings
from app.rss.errors import RssError
from app.rss.fetcher import FeedEntry, FeedResult, parse_feed
from app.rss.publisher import PublishResult
from app.rss.schemas import SubscriptionInput
from app.rss.service import RssService
from app.rss.worker import RssWorker
from app.storage.db import Database
from app.storage.models import RssEntry, RssSubscription, utc_now


class FakeFetcher:
    def __init__(self, result=None) -> None:
        self.result = result or FeedResult()
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

    def __init__(self) -> None:
        self.calls = []
        self.error = None
        self.entered = asyncio.Event()
        self.release = None
        self.closed = False

    async def send(self, chat_id, text, link=""):
        self.calls.append((chat_id, text, link))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return PublishResult(len(self.calls), -100987)

    async def stop(self):
        self.closed = True


class GatedFetcher(FakeFetcher):
    """仅阻塞指定 URL 的抓取，用于验证不同订阅的任务互不阻塞。"""

    def __init__(self, result, gate_url: str) -> None:
        super().__init__(result)
        self.gate_url = gate_url
        self.gate = asyncio.Event()
        self.gated = asyncio.Event()

    async def fetch(self, url, **kwargs):
        if url == self.gate_url:
            self.gated.set()
            await self.gate.wait()
        return await super().fetch(url, **kwargs)


def entry(number: int, **kwargs) -> FeedEntry:
    return FeedEntry(
        **{
            "entry_key": f"guid-{number}",
            "title": f"Entry {number}",
            "link": f"https://example.com/{number}",
            "summary": "Safe summary",
            "published_at": utc_now() + timedelta(seconds=number),
            **kwargs,
        }
    )


def payload(**kwargs) -> SubscriptionInput:
    return SubscriptionInput(
        **{
            "name": "RSS",
            "feed_url": "https://example.com/rss",
            "tg_chat_id": "-100123",
            **kwargs,
        }
    )


@pytest.fixture
async def context(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'rss.db'}")
    await db.init()
    settings = Settings(_env_file=None, rss_send_interval_seconds=0.01, rss_translate_enabled=False)
    fetcher = FakeFetcher(FeedResult("Feed", [entry(1), entry(2), entry(3)]))
    publisher = FakePublisher()
    service = RssService(db, settings, fetcher)
    worker = RssWorker(settings, service, publisher)
    try:
        yield db, settings, fetcher, publisher, service, worker
    finally:
        await worker.stop()
        await db.dispose()


async def due(db, subscription_id, *, retry=False) -> None:
    async with db.session() as session:
        sub = await session.get(RssSubscription, subscription_id)
        sub.next_check_at = utc_now() - timedelta(seconds=1)
        if retry:
            rows = (
                await session.execute(
                    select(RssEntry).where(
                        RssEntry.subscription_id == subscription_id, RssEntry.status == "pending"
                    )
                )
            ).scalars()
            for row in rows:
                row.next_retry_at = utc_now() - timedelta(seconds=1)


async def test_first_skip_and_new_entries_only(context) -> None:
    db, _, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload())
    await worker.process_subscription(sub["id"])
    assert publisher.calls == []
    assert {e["status"] for e in await service.list_entries(sub["id"])} == {"skipped"}
    first = await service.get_subscription(sub["id"])
    assert first["initialized"] and first["last_success_at"].tzinfo is not None
    fetcher.result.entries = [entry(1), entry(2), entry(3), entry(4)]
    await due(db, sub["id"])
    await worker.process_subscription(sub["id"])
    assert len(publisher.calls) == 1 and publisher.calls[0][2].endswith("/4")
    sent = (await service.list_entries(sub["id"]))[0]
    assert sent["status"] == "sent" and sent["tg_chat_id"] == -100987
    assert sent["tg_message_id"] == 1 and sent["sent_at"].tzinfo is not None


async def test_first_latest_order_initial_count_and_filters(context) -> None:
    _, _, fetcher, publisher, service, worker = context
    fetcher.result.entries = [entry(i) for i in range(1, 6)]
    sub = await service.create_subscription(
        payload(initial_policy="latest", initial_count=3, exclude_keywords=["Entry 4"])
    )
    await worker.process_subscription(sub["id"])
    assert [call[2].rsplit("/", 1)[1] for call in publisher.calls] == ["3", "5"]
    assert (await service.get_subscription(sub["id"]))["sent_count"] == 2


async def test_restart_and_same_guid_updates_do_not_resend(context) -> None:
    db, settings, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    restarted = RssWorker(settings, RssService(db, settings, fetcher), publisher)
    fetcher.result.entries = [entry(1, title="Changed"), entry(2), entry(3)]
    await restarted.process_subscription(sub["id"], force=True)
    assert len(publisher.calls) == 3
    assert len(await service.list_entries(sub["id"])) == 3


async def test_pending_survives_feed_disappearance_fetch_failure_and_304(context) -> None:
    db, _, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=1))
    publisher.error = NetworkError("token-secret")
    await worker.process_subscription(sub["id"])
    pending = (await service.list_entries(sub["id"]))[0]
    assert pending["status"] == "pending" and pending["attempts"] == 1
    assert pending["next_retry_at"].tzinfo is not None
    assert "token-secret" not in pending["last_error"]
    publisher.error = None
    fetcher.error = RuntimeError("https://example.com?token=secret")
    await due(db, sub["id"], retry=True)
    await worker.process_subscription(sub["id"])
    assert (await service.list_entries(sub["id"]))[0]["status"] == "sent"
    assert (await service.get_subscription(sub["id"]))["failure_count"] == 1
    assert len(publisher.calls) == 2
    fetcher.error = None
    fetcher.result = FeedResult(not_modified=True)
    await worker.process_subscription(sub["id"], force=True)
    assert (await service.get_subscription(sub["id"]))["initialized"]
    assert len(publisher.calls) == 2


async def test_cache_304_preserves_initialized_and_first_304_does_not_initialize(context) -> None:
    _, _, fetcher, _, service, worker = context
    sub = await service.create_subscription(payload())
    fetcher.result = FeedResult(not_modified=True)
    await worker.process_subscription(sub["id"])
    assert not (await service.get_subscription(sub["id"]))["initialized"]
    fetcher.result = FeedResult("RSS", [], etag='"etag"', last_modified="modified")
    await worker.process_subscription(sub["id"], force=True)
    assert (await service.get_subscription(sub["id"]))["initialized"]
    fetcher.result = FeedResult(not_modified=True)
    await worker.process_subscription(sub["id"], force=True)
    assert fetcher.calls[-1][1] == {"etag": '"etag"', "last_modified": "modified"}
    stored = await service.get_subscription(sub["id"])
    assert stored["etag"] == '"etag"' and stored["last_modified"] == "modified"
    assert stored["initialized"] and stored["feed_title"] == "RSS"


async def test_pause_callback_and_disabled_keep_pending(context) -> None:
    db, _, fetcher, publisher, service, worker = context
    allowed = False

    async def can_publish():
        return allowed

    worker.can_publish = can_publish
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    assert len(fetcher.calls) == 1 and not publisher.calls
    assert (await service.get_subscription(sub["id"]))["pending_count"] == 3
    await service.set_enabled(sub["id"], False)
    await worker.process_subscription(sub["id"], force=True)
    assert len(fetcher.calls) == 1
    for action in (worker.trigger_check, worker.retry_failed):
        with pytest.raises(RssError) as error:
            await action(sub["id"])
        assert error.value.status_code == 409
    allowed = True
    await service.set_enabled(sub["id"], True)
    await due(db, sub["id"])
    await worker.process_subscription(sub["id"])
    assert len(publisher.calls) == 3


async def test_global_disable_and_missing_publisher(context) -> None:
    _, settings, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    settings.rss_enabled = False
    await worker.process_subscription(sub["id"], force=True)
    assert fetcher.calls == [] and publisher.calls == []
    settings.rss_enabled = True
    publisher.configured = False
    await worker.process_subscription(sub["id"])
    assert len(fetcher.calls) == 1 and publisher.calls == []
    assert (await service.status())["pending_count"] == 3


async def test_concurrent_checks_share_lock_and_no_duplicate(context) -> None:
    _, _, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    fetcher.release = asyncio.Event()
    first = asyncio.create_task(worker.process_subscription(sub["id"]))
    await fetcher.entered.wait()
    second = asyncio.create_task(worker.process_subscription(sub["id"]))
    fetcher.release.set()
    await asyncio.gather(first, second)
    assert len(fetcher.calls) == 1 and len(publisher.calls) == 3


async def test_different_subscriptions_do_not_block_each_other(context) -> None:
    db, settings, _, publisher, _, _ = context
    fetcher = GatedFetcher(FeedResult("Feed", [entry(1)]), "https://example.com/slow")
    service = RssService(db, settings, fetcher)
    worker = RssWorker(settings, service, publisher)
    try:
        slow = await service.create_subscription(
            payload(feed_url="https://example.com/slow", initial_policy="latest")
        )
        fast = await service.create_subscription(
            payload(feed_url="https://example.com/fast", initial_policy="latest")
        )
        process = asyncio.create_task(worker.process_subscription(slow["id"]))
        try:
            await asyncio.wait_for(fetcher.gated.wait(), timeout=1)
            # 慢订阅抓取期间，另一订阅的抓取与发送不被阻塞。
            await asyncio.wait_for(worker.process_subscription(fast["id"]), timeout=1)
            assert len(publisher.calls) == 1
            assert not process.done()
        finally:
            fetcher.gate.set()
            await process
        assert len(publisher.calls) == 2
        assert (await service.get_subscription(slow["id"]))["sent_count"] == 1
        assert (await service.get_subscription(fast["id"]))["sent_count"] == 1
    finally:
        fetcher.gate.set()


@pytest.mark.parametrize("error", [BadRequest("private-token"), Forbidden("private-token")])
async def test_permanent_failure_and_manual_retry(context, error) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=1))
    publisher.error = error
    await worker.process_subscription(sub["id"])
    failed = (await service.list_entries(sub["id"]))[0]
    assert failed["status"] == "failed" and failed["next_retry_at"] is None
    assert "private-token" not in failed["last_error"]
    await worker.process_subscription(sub["id"])
    assert len(publisher.calls) == 1
    publisher.error = None
    response = await worker.retry_failed(sub["id"])
    assert response["retried_count"] == 1
    await worker._jobs[sub["id"]]
    assert len(publisher.calls) == 2
    assert (await service.get_subscription(sub["id"]))["sent_count"] == 1


async def test_render_failure_marks_failed_without_send(context) -> None:
    _, _, fetcher, publisher, service, worker = context
    fetcher.result = FeedResult(
        "Feed", [entry(1, link="https://example.com/" + "a" * 4200)]
    )
    sub = await service.create_subscription(
        payload(initial_policy="latest", message_template="{link}")
    )
    await worker.process_subscription(sub["id"])
    assert publisher.calls == []
    row = (await service.list_entries(sub["id"]))[0]
    assert row["status"] == "failed" and row["last_error"] == "RSS 消息模板或长度无效"
    assert row["next_retry_at"] is None


@pytest.mark.parametrize("raw", [7, timedelta(seconds=7)])
async def test_retry_after_seconds_persisted(context, raw) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=1))
    publisher.error = RetryAfter(raw)
    before = utc_now()
    await worker.process_subscription(sub["id"])
    row = (await service.list_entries(sub["id"]))[0]
    assert row["status"] == "pending" and row["attempts"] == 1
    assert (row["next_retry_at"] - before).total_seconds() >= 7
    await worker.process_subscription(sub["id"])
    assert len(publisher.calls) == 1


async def test_transient_failure_retry_cap(context) -> None:
    db, settings, _, publisher, service, worker = context
    settings.rss_max_send_attempts = 2
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=1))
    publisher.error = NetworkError("secret")
    await worker.process_subscription(sub["id"])
    await due(db, sub["id"], retry=True)
    await worker.process_subscription(sub["id"])
    assert (await service.list_entries(sub["id"]))[0]["status"] == "failed"
    await worker.process_subscription(sub["id"], force=True)
    assert len(publisher.calls) == 2


async def test_update_target_pending_uses_new_target_and_sent_not_repeated(context) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    publisher.configured = False
    await worker.process_subscription(sub["id"])
    await service.update_subscription(
        sub["id"], payload(initial_policy="latest", tg_chat_id="@new_channel")
    )
    publisher.configured = True
    await worker.process_subscription(sub["id"])
    assert len(publisher.calls) == 3 and {c[0] for c in publisher.calls} == {"@new_channel"}
    await service.update_subscription(
        sub["id"], payload(initial_policy="latest", tg_chat_id="-200")
    )
    await worker.process_subscription(sub["id"])
    assert len(publisher.calls) == 3


async def test_update_feed_cancels_old_pending_and_resets_cache(context) -> None:
    _, _, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    publisher.configured = False
    fetcher.result.etag = '"cached"'
    await worker.process_subscription(sub["id"])
    updated = await service.update_subscription(
        sub["id"],
        payload(
            feed_url="https://example.com/other",
            initial_policy="skip",
        ),
    )
    assert not updated["initialized"] and updated["etag"] is None
    assert updated["last_success_at"] is None and updated["pending_count"] == 0
    assert {e["status"] for e in await service.list_entries(sub["id"])} == {"skipped"}
    publisher.configured = True
    await worker.process_subscription(sub["id"])
    assert not publisher.calls
    assert len(await service.list_entries(sub["id"])) == 6


async def test_delete_cascades_and_not_found_contract(context) -> None:
    db, _, _, _, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    assert await service.delete_subscription(sub["id"])
    assert await service.list_subscriptions() == []
    async with db.session() as session:
        assert not (await session.execute(select(RssEntry))).scalars().all()
    for action in (
        service.get_subscription,
        service.list_entries,
        service.delete_subscription,
        worker.trigger_check,
        worker.retry_failed,
    ):
        with pytest.raises(RssError) as error:
            await action(sub["id"])
        assert error.value.code == "rss_not_found" and error.value.status_code == 404


@pytest.mark.parametrize("error", [None, NetworkError("secret"), RetryAfter(2)])
async def test_delete_during_send_does_not_wait_crash_or_resurrect(context, error) -> None:
    db, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=3))
    publisher.release = asyncio.Event()
    publisher.error = error
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(publisher.entered.wait(), timeout=1)
        assert await asyncio.wait_for(service.delete_subscription(sub["id"]), timeout=1)
        assert not process.done()
    finally:
        publisher.release.set()
        await process
    assert len(publisher.calls) == 1
    async with db.session() as session:
        assert not (await session.execute(select(RssEntry))).scalars().all()
    assert worker.status_snapshot()["last_worker_error"] is None


async def test_send_one_after_delete_does_not_resurrect(context) -> None:
    db, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    publisher.configured = False
    await worker.process_subscription(sub["id"])
    publisher.configured = True
    entry_id = (await service.list_entries(sub["id"]))[0]["id"]
    assert await service.delete_subscription(sub["id"])
    await worker._send_one(sub["id"], entry_id)
    assert publisher.calls == []
    async with db.session() as session:
        assert not (await session.execute(select(RssSubscription))).scalars().all()
        assert not (await session.execute(select(RssEntry))).scalars().all()
    assert worker.status_snapshot()["last_worker_error"] is None


async def test_send_one_after_disable_skips_without_error(context) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    publisher.configured = False
    await worker.process_subscription(sub["id"])
    await service.set_enabled(sub["id"], False)
    publisher.configured = True
    entry_id = (await service.list_entries(sub["id"]))[0]["id"]
    await worker._send_one(sub["id"], entry_id)
    assert publisher.calls == []
    row = (await service.list_entries(sub["id"]))[0]
    assert row["status"] == "pending" and row["attempts"] == 0
    assert worker.status_snapshot()["last_worker_error"] is None


async def test_cancellation_sending_recovery_acknowledges_unknown(context) -> None:
    db, settings, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=1))
    publisher.release = asyncio.Event()
    task = asyncio.create_task(worker.process_subscription(sub["id"]))
    await publisher.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await service.list_entries(sub["id"]))[0]["status"] == "sending"
    publisher.release = None
    publisher.configured = False
    restarted = RssWorker(settings, service, publisher)
    await restarted.start()
    try:
        row = (await service.list_entries(sub["id"]))[0]
        assert row["status"] == "pending" and row["attempts"] == 1
        assert "可能重复" in row["last_error"]
        assert restarted.status_snapshot()["worker_running"]
    finally:
        await restarted.stop()
    assert not restarted.status_snapshot()["worker_running"]
    assert publisher.closed
    async with db.session() as session:
        assert (await session.get(RssEntry, row["id"])).status == "pending"


async def test_trigger_is_background_and_start_idempotent(context) -> None:
    _, _, fetcher, _, service, worker = context
    sub = await service.create_subscription(payload())
    fetcher.release = asyncio.Event()
    response = await worker.trigger_check(sub["id"])
    assert response["scheduled"] and not fetcher.entered.is_set()
    await fetcher.entered.wait()
    fetcher.release.set()
    await worker._jobs[sub["id"]]
    await worker.start()
    task = worker._task
    await worker.start()
    assert worker._task is task


async def test_status_preview_counts_and_no_secrets(context) -> None:
    _, settings, fetcher, _, service, worker = context
    settings.rss_bot_token = "rss-secret"
    settings.tg_admin_bot_token = "admin-secret"
    preview = await service.preview(payload(message_template="{title}"))
    assert preview["feed_title"] == "Feed" and len(preview["entries"]) == 3
    assert preview["entries"][0]["matches"]
    assert preview["entries"][0]["rendered_text"].endswith("https://example.com/1")
    assert await service.list_subscriptions() == []
    sub = await service.create_subscription(payload(initial_policy="latest"))
    await worker.process_subscription(sub["id"])
    status = await service.status()
    assert status == {
        "enabled": True,
        "publisher_configured": True,
        "total_subscriptions": 1,
        "enabled_subscriptions": 1,
        "pending_count": 0,
        "failed_count": 0,
        "sent_count": 3,
    }
    assert "secret" not in repr(await service.list_subscriptions())
    fetcher.error = RuntimeError("rss-secret")
    with pytest.raises(RssError) as error:
        await service.preview(payload())
    assert "rss-secret" not in str(error.value)
    with pytest.raises(RssError):
        await service.list_entries(sub["id"], limit=0)


async def test_unique_constraint_and_fetch_backoff_cap(context) -> None:
    db, _, fetcher, _, service, worker = context
    sub = await service.create_subscription(payload())
    await worker.process_subscription(sub["id"])
    rows = await service.list_entries(sub["id"])
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            session.add(
                RssEntry(
                    subscription_id=sub["id"],
                    entry_key=rows[0]["entry_key"],
                    title="Duplicate",
                    link="",
                    summary="",
                )
            )
    fetcher.error = RuntimeError("token")
    before = utc_now()
    await worker.process_subscription(sub["id"], force=True)
    first = await service.get_subscription(sub["id"])
    assert (first["next_check_at"] - before).total_seconds() >= 1200
    async with db.session() as session:
        stored = await session.get(RssSubscription, sub["id"])
        stored.failure_count = 30
    await worker.process_subscription(sub["id"], force=True)
    stored = await service.get_subscription(sub["id"])
    assert 86399 <= (stored["next_check_at"] - utc_now()).total_seconds() <= 86400


async def test_slow_fetch_does_not_block_list_or_disable(context) -> None:
    _, _, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    fetcher.release = asyncio.Event()
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(fetcher.entered.wait(), timeout=1)
        listed = await asyncio.wait_for(service.list_subscriptions(), timeout=1)
        assert listed[0]["id"] == sub["id"]
        await asyncio.wait_for(service.set_enabled(sub["id"], False), timeout=1)
        assert not process.done()
    finally:
        fetcher.release.set()
        await process
    assert publisher.calls == []
    stored = await service.get_subscription(sub["id"])
    assert not stored["initialized"] and stored["last_success_at"] is None
    assert await service.list_entries(sub["id"]) == []


async def test_slow_fetch_does_not_block_delete(context) -> None:
    _, _, fetcher, _, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    fetcher.release = asyncio.Event()
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(fetcher.entered.wait(), timeout=1)
        assert await asyncio.wait_for(service.delete_subscription(sub["id"]), timeout=1)
        assert await asyncio.wait_for(service.list_subscriptions(), timeout=1) == []
        assert not process.done()
    finally:
        fetcher.release.set()
        await process
    assert await service.list_subscriptions() == []
    assert worker.status_snapshot()["last_worker_error"] is None


async def test_disable_during_send_stops_subsequent_entries(context) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    publisher.release = asyncio.Event()
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(publisher.entered.wait(), timeout=1)
        listed = await asyncio.wait_for(service.list_subscriptions(), timeout=1)
        assert listed[0]["pending_count"] == 3
        await asyncio.wait_for(service.set_enabled(sub["id"], False), timeout=1)
    finally:
        publisher.release.set()
        await process
    assert len(publisher.calls) == 1
    sub = await service.get_subscription(sub["id"])
    assert sub["sent_count"] == 1 and sub["pending_count"] == 2 and not sub["enabled"]


async def test_disable_during_rate_limit_does_not_block_crud(context, monkeypatch) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def limit_sleep(delay):
        entered.set()
        await release.wait()

    worker._next_send_at = asyncio.get_running_loop().time() + 60
    monkeypatch.setattr("app.rss.worker.asyncio.sleep", limit_sleep)
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(service.set_enabled(sub["id"], False), timeout=1)
        assert (await asyncio.wait_for(service.list_subscriptions(), timeout=1))[0][
            "pending_count"
        ] == 3
    finally:
        release.set()
        await process
    assert publisher.calls == []


async def test_target_change_during_send_affects_only_following_pending(context) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    publisher.release = asyncio.Event()
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(publisher.entered.wait(), timeout=1)
        await asyncio.wait_for(
            service.update_subscription(
                sub["id"], payload(initial_policy="latest", tg_chat_id="@new_channel")
            ),
            timeout=1,
        )
    finally:
        publisher.release.set()
        await process
    assert [call[0] for call in publisher.calls] == ["-100123", "@new_channel", "@new_channel"]
    assert (await service.get_subscription(sub["id"]))["sent_count"] == 3


@pytest.mark.parametrize("error", [None, NetworkError("secret")])
async def test_feed_change_during_send_does_not_revive_skipped(context, error) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    publisher.release = asyncio.Event()
    publisher.error = error
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(publisher.entered.wait(), timeout=1)
        await asyncio.wait_for(
            service.update_subscription(sub["id"], payload(feed_url="https://example.com/new")),
            timeout=1,
        )
    finally:
        publisher.release.set()
        await process
    assert len(publisher.calls) == 1
    rows = await service.list_entries(sub["id"])
    assert {row["status"] for row in rows} == {"skipped"}
    assert all(row["sent_at"] is None and row["next_retry_at"] is None for row in rows)
    assert all(row["last_error"] == "订阅源已更换" for row in rows)


@pytest.mark.parametrize("kind", ["feed", "same_url_new_version", "304", "error", "delete"])
async def test_stale_fetch_result_discarded_after_update_or_delete(context, kind) -> None:
    _, _, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    fetcher.result.etag = '"old-cache"'
    if kind == "304":
        fetcher.result = FeedResult(not_modified=True, etag='"old-cache"')
    if kind == "error":
        fetcher.error = RssError("rss_invalid_feed", 422, "内容不是有效订阅")
    fetcher.release = asyncio.Event()
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(fetcher.entered.wait(), timeout=1)
        if kind == "delete":
            await asyncio.wait_for(service.delete_subscription(sub["id"]), timeout=1)
        else:
            await asyncio.wait_for(
                service.update_subscription(sub["id"], payload(feed_url="https://example.com/new")),
                timeout=1,
            )
            if kind == "same_url_new_version":
                await service.update_subscription(sub["id"], payload())
    finally:
        fetcher.release.set()
        await process
    assert publisher.calls == []
    if kind == "delete":
        assert await service.list_subscriptions() == []
    else:
        stored = await service.get_subscription(sub["id"])
        assert not stored["initialized"] and stored["etag"] is None
        assert stored["last_success_at"] is None and stored["last_error"] is None
        assert stored["last_checked_at"] is None and stored["failure_count"] == 0
        assert await service.list_entries(sub["id"]) == []


async def test_can_publish_callback_does_not_hold_crud_lock(context) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def can_publish():
        entered.set()
        await release.wait()
        return True

    worker.can_publish = can_publish
    process = asyncio.create_task(worker.process_subscription(sub["id"]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(service.set_enabled(sub["id"], False), timeout=1)
        assert (await asyncio.wait_for(service.get_subscription(sub["id"]), timeout=1))[
            "pending_count"
        ] == 3
    finally:
        release.set()
        await process
    assert publisher.calls == []


async def test_concurrent_job_does_not_recover_inflight_sending(context) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=1))
    publisher.release = asyncio.Event()
    first = asyncio.create_task(worker.process_subscription(sub["id"]))
    second = None
    try:
        await asyncio.wait_for(publisher.entered.wait(), timeout=1)
        second = asyncio.create_task(worker.process_subscription(sub["id"], force=True))
        assert (await service.list_entries(sub["id"]))[0]["status"] == "sending"
        assert len(publisher.calls) == 1
    finally:
        publisher.release.set()
        await first
        if second is not None:
            await second
    assert len(publisher.calls) == 1
    assert (await service.list_entries(sub["id"]))[0]["attempts"] == 1


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            RssError("rss_private_host", 422, "订阅地址禁止访问非公网主机"),
            "rss_private_host: 订阅地址禁止访问非公网主机",
        ),
        (
            RssError("rss_invalid_feed", 422, "订阅地址返回 HTML 而非 RSS/Atom"),
            "rss_invalid_feed: 订阅地址返回 HTML 而非 RSS/Atom",
        ),
        (RuntimeError("https://example.com?token=secret"), "RSS 抓取失败"),
    ],
)
async def test_safe_fetch_error_code_and_message_persisted(context, error, expected) -> None:
    _, _, fetcher, _, service, worker = context
    sub = await service.create_subscription(payload())
    fetcher.error = error
    await worker.process_subscription(sub["id"])
    stored = await service.get_subscription(sub["id"])
    assert stored["last_error"] == expected and stored["failure_count"] == 1
    assert "secret" not in stored["last_error"]


async def test_no_date_first_latest_and_feed_cap_choose_newest(context) -> None:
    _, _, fetcher, publisher, service, worker = context
    body = (
        '<rss version="2.0"><channel><title>RSS</title>'
        + "".join(
            f"<item><guid>{n}</guid><title>Entry {n}</title><link>https://example.com/{n}</link></item>"
            for n in range(6, 0, -1)
        )
        + "</channel></rss>"
    )
    fetcher.result = parse_feed(body.encode(), "https://example.com/rss", max_entries=4)
    assert [e.title for e in fetcher.result.entries] == ["Entry 3", "Entry 4", "Entry 5", "Entry 6"]
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=2))
    await worker.process_subscription(sub["id"])
    assert [call[2] for call in publisher.calls] == [
        "https://example.com/5",
        "https://example.com/6",
    ]


async def test_trigger_during_slow_fetch_retains_followup_check(context) -> None:
    _, _, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload(initial_policy="latest", initial_count=1))
    fetcher.release = asyncio.Event()
    await worker.trigger_check(sub["id"])
    task = worker._jobs[sub["id"]]
    try:
        await asyncio.wait_for(fetcher.entered.wait(), timeout=1)
        response = await asyncio.wait_for(worker.trigger_check(sub["id"]), timeout=1)
        assert response["scheduled"] and worker._jobs[sub["id"]] is task
    finally:
        fetcher.release.set()
        await task
    assert len(fetcher.calls) == 2 and len(publisher.calls) == 1


async def test_preview_only_latest_ten_without_baseline_side_effects(context) -> None:
    _, _, fetcher, publisher, service, worker = context
    sub = await service.create_subscription(payload())
    await worker.process_subscription(sub["id"])
    before = await service.get_subscription(sub["id"])
    history = await service.list_entries(sub["id"])
    fetcher.result = FeedResult("Preview", [entry(n) for n in range(1, 501)])
    result = await service.preview(payload(initial_policy="latest"))
    assert len(result["entries"]) == 10
    assert [row["title"] for row in result["entries"]] == [f"Entry {n}" for n in range(491, 501)]
    assert any("预览仅展示最近 10 条" in warning for warning in result["warnings"])
    assert await service.get_subscription(sub["id"]) == before
    assert await service.list_entries(sub["id"]) == history
    assert publisher.calls == [] and len(fetcher.result.entries) == 500


async def test_skip_reason_recorded_for_baseline_and_keywords(context) -> None:
    _, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(include_keywords=["Entry 2"]))
    await worker.process_subscription(sub["id"])
    rows = {row["title"]: row for row in await service.list_entries(sub["id"])}
    assert rows["Entry 1"]["skip_reason"] == "未命中包含关键词"
    assert rows["Entry 3"]["skip_reason"] == "未命中包含关键词"
    assert rows["Entry 2"]["skip_reason"] == "首次基线未发送"
    assert publisher.calls == []


async def test_pending_not_matching_keywords_is_not_sent(context) -> None:
    db, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(include_keywords=["Entry 2"]))
    await worker.process_subscription(sub["id"])
    # 模拟旧关键词队列里遗留的 pending：正文已不再命中当前关键词。
    async with db.session() as session:
        row = (
            await session.execute(
                select(RssEntry).where(
                    RssEntry.subscription_id == sub["id"], RssEntry.title == "Entry 1"
                )
            )
        ).scalar_one()
        row.status = "pending"
        row.skip_reason = None
    await worker.process_subscription(sub["id"])
    assert publisher.calls == []
    rows = {row["title"]: row for row in await service.list_entries(sub["id"])}
    assert rows["Entry 1"]["status"] == "skipped"
    assert rows["Entry 1"]["skip_reason"] == "未命中包含关键词"
    assert rows["Entry 1"]["attempts"] == 0


async def test_update_keywords_withdraws_pending_queue(context) -> None:
    db, _, _, publisher, service, worker = context
    sub = await service.create_subscription(payload(include_keywords=["Entry 1"]))
    await worker.process_subscription(sub["id"])
    async with db.session() as session:
        row = (
            await session.execute(
                select(RssEntry).where(
                    RssEntry.subscription_id == sub["id"], RssEntry.title == "Entry 1"
                )
            )
        ).scalar_one()
        row.status = "pending"
        row.skip_reason = None
    await service.update_subscription(sub["id"], payload(include_keywords=["Entry 9"]))
    rows = {row["title"]: row for row in await service.list_entries(sub["id"])}
    assert rows["Entry 1"]["status"] == "skipped"
    assert rows["Entry 1"]["skip_reason"] == "未命中包含关键词"
    assert publisher.calls == []


async def test_reevaluate_keywords_requeues_only_keyword_skips(context) -> None:
    _, _, _, _, service, worker = context
    sub = await service.create_subscription(payload(include_keywords=["Entry 2"]))
    await worker.process_subscription(sub["id"])
    # 首次基线跳过的历史不补发。
    first = await worker.reevaluate_keywords(sub["id"])
    assert first["requeued_count"] == 0 and first["kept_count"] == 3
    await service.update_subscription(sub["id"], payload(include_keywords=["Entry 1"]))
    second = await worker.reevaluate_keywords(sub["id"])
    assert second["requeued_count"] == 1
    rows = {row["title"]: row for row in await service.list_entries(sub["id"])}
    assert rows["Entry 1"]["status"] == "pending"
    assert rows["Entry 1"]["skip_reason"] is None
    assert rows["Entry 2"]["status"] == "skipped"
