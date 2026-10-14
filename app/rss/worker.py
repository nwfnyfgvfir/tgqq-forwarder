from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from app.config import Settings
from app.rss.errors import RssError
from app.rss.formatting import matches_keywords, render_entry
from app.rss.publisher import RssPublisher
from app.rss.service import RssService
from app.rss.translator import AITranslator
from app.storage.models import RssEntry, RssSubscription, utc_now

logger = logging.getLogger(__name__)


class RssWorker:
    """进程内单实例调度器；响应丢失或 sending 恢复可能重复发送，非 exactly-once。"""

    def __init__(
        self,
        settings: Settings,
        service: RssService,
        publisher=None,
        can_publish: Callable[[], Awaitable[bool]] | None = None,
        translator=None,
    ) -> None:
        self.settings = settings
        self.service = service
        self.publisher = publisher if publisher is not None else RssPublisher(settings)
        self.can_publish = can_publish
        self.translator = translator if translator is not None else AITranslator(settings)
        self._task: asyncio.Task | None = None
        self._jobs: dict[int, asyncio.Task] = {}
        self._job_locks: dict[int, asyncio.Lock] = {}
        self._check_requests: set[int] = set()
        self._wake = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._next_send_at = 0.0
        self._last_worker_error: str | None = None

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._task is not None and not self._task.done():
                return
            async with self.service.db.session() as session:
                ids = list((await session.execute(select(RssSubscription.id))).scalars())
            for subscription_id in ids:
                async with self._job_lock(subscription_id):
                    await self._recover_sending(subscription_id)
            self._last_worker_error = None
            self._task = asyncio.create_task(self._scheduler(), name="rss-scheduler")
            self._wake.set()

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            tasks = ([self._task] if self._task else []) + list(self._jobs.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._task = None
            self._jobs.clear()
            self._check_requests.clear()
            try:
                await self.publisher.stop()
            except Exception:
                self._last_worker_error = "RSS 发布连接关闭失败"
            close = getattr(self.service.fetcher, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    self._last_worker_error = "RSS 抓取连接关闭失败"
            # 发布与预览各自持有 translator，逐一关闭；重复关闭是幂等的。
            for translator in (self.translator, getattr(self.service, "translator", None)):
                close = getattr(translator, "close", None)
                if close is None:
                    continue
                try:
                    await close()
                except Exception:
                    self._last_worker_error = "RSS 翻译连接关闭失败"

    def status_snapshot(self) -> dict:
        return {
            "worker_running": self._task is not None and not self._task.done(),
            "last_worker_error": self._last_worker_error,
        }

    async def _require_enabled(self, session, subscription_id: int) -> RssSubscription:
        sub = await self.service._get(session, subscription_id)
        if not sub.enabled or not self.settings.rss_enabled:
            raise RssError("rss_disabled", 409, "RSS 订阅或功能已禁用")
        return sub

    async def trigger_check(self, subscription_id: int) -> dict:
        async with (
            self.service.subscription_lock(subscription_id),
            self.service.db.session() as session,
        ):
            sub = await self._require_enabled(session, subscription_id)
            sub.next_check_at = None
        self._check_requests.add(subscription_id)
        self._schedule(subscription_id)
        return {"scheduled": True, "subscription_id": subscription_id}

    async def retry_failed(self, subscription_id: int) -> dict:
        async with (
            self.service.subscription_lock(subscription_id),
            self.service.db.session() as session,
        ):
            await self._require_enabled(session, subscription_id)
            result = await session.execute(
                update(RssEntry)
                .where(
                    RssEntry.subscription_id == subscription_id,
                    RssEntry.status == "failed",
                )
                .values(status="pending", attempts=0, last_error=None, next_retry_at=None)
            )
            count = result.rowcount
        self._schedule(subscription_id)
        return {"scheduled": True, "subscription_id": subscription_id, "retried_count": count}

    def _schedule(self, subscription_id: int) -> None:
        task = self._jobs.get(subscription_id)
        if task is not None and not task.done():
            self._wake.set()
            return
        self._jobs[subscription_id] = asyncio.create_task(
            self._run_job(subscription_id), name=f"rss-subscription-{subscription_id}"
        )

    async def _run_job(self, subscription_id: int) -> None:
        try:
            while True:
                force = subscription_id in self._check_requests
                self._check_requests.discard(subscription_id)
                await self.process_subscription(subscription_id, force=force)
                # 网络期间收到的手动请求不能被旧结果的 next_check_at 覆盖而丢失。
                if subscription_id not in self._check_requests:
                    break
        except RssError as error:
            if error.code != "rss_not_found":
                self._last_worker_error = "RSS 订阅处理失败"
        except Exception:
            self._last_worker_error = "RSS 订阅处理失败"

    async def _scheduler(self) -> None:
        while True:
            try:
                if self.settings.rss_enabled:
                    async with self.service.db.session() as session:
                        now = utc_now()
                        pending_ids = select(RssEntry.subscription_id).where(
                            RssEntry.status.in_(["pending", "sending"]),
                            (RssEntry.next_retry_at.is_(None)) | (RssEntry.next_retry_at <= now),
                        )
                        ids = list(
                            (
                                await session.execute(
                                    select(RssSubscription.id).where(
                                        RssSubscription.enabled.is_(True),
                                        (RssSubscription.next_check_at.is_(None))
                                        | (RssSubscription.next_check_at <= now)
                                        | RssSubscription.id.in_(pending_ids),
                                    )
                                )
                            ).scalars()
                        )
                    for subscription_id in ids:
                        self._schedule(subscription_id)
                # 清理已完成 task 引用，异常已由 _run_job 消费。
                self._jobs = {key: task for key, task in self._jobs.items() if not task.done()}
                self._wake.clear()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.settings.rss_poll_seconds
                    )
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                self._last_worker_error = "RSS 调度失败"
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.settings.rss_poll_seconds
                    )
                except TimeoutError:
                    pass

    def _job_lock(self, subscription_id: int) -> asyncio.Lock:
        return self._job_locks.setdefault(subscription_id, asyncio.Lock())

    async def _recover_sending(self, subscription_id: int) -> None:
        async with (
            self.service.subscription_lock(subscription_id),
            self.service.db.session() as session,
        ):
            await session.execute(
                update(RssEntry)
                .where(
                    RssEntry.subscription_id == subscription_id,
                    RssEntry.status == "sending",
                )
                .values(
                    status="pending",
                    last_error="上次发送结果未知，恢复后可能重复",
                    next_retry_at=None,
                )
            )

    async def process_subscription(self, subscription_id: int, force: bool = False) -> None:
        async with self._job_lock(subscription_id):
            async with (
                self.service.subscription_lock(subscription_id),
                self.service.db.session() as session,
            ):
                sub = await self.service._get(session, subscription_id)
            if not sub.enabled or not self.settings.rss_enabled:
                return
            await self._recover_sending(subscription_id)
            if force or sub.next_check_at is None or sub.next_check_at <= utc_now():
                await self._check_feed(sub)
            # 抓取失败/304 仍处理已持久化且源中已消失的 pending。
            await self._publish_pending(subscription_id)

    def _same_feed(self, stored: RssSubscription | None, snapshot: RssSubscription) -> bool:
        return bool(
            stored is not None
            and stored.enabled
            and self.settings.rss_enabled
            and stored.feed_url == snapshot.feed_url
            and stored.feed_version == snapshot.feed_version
            and stored.created_at == snapshot.created_at
        )

    async def _check_feed(self, sub: RssSubscription) -> None:
        async with (
            self.service.subscription_lock(sub.id),
            self.service.db.session() as session,
        ):
            stored = await session.get(RssSubscription, sub.id)
            if not self._same_feed(stored, sub):
                return
            stored.last_checked_at = utc_now()
            sub = stored
        # 抓取期间仅持有 worker job 锁，不阻塞列表、停用、更新和删除。
        try:
            result = await self.service.fetcher.fetch(
                sub.feed_url, etag=sub.etag, last_modified=sub.last_modified
            )
        except Exception as error:
            async with (
                self.service.subscription_lock(sub.id),
                self.service.db.session() as session,
            ):
                stored = await session.get(RssSubscription, sub.id)
                if not self._same_feed(stored, sub):
                    return
                stored.failure_count += 1
                stored.last_error = (
                    f"{error.code}: {error.message}"
                    if isinstance(error, RssError)
                    else "RSS 抓取失败"
                )
                delay = min(86400, stored.interval_seconds * 2 ** min(stored.failure_count, 16))
                stored.next_check_at = utc_now() + timedelta(seconds=delay)
            return
        async with (
            self.service.subscription_lock(sub.id),
            self.service.db.session() as session,
        ):
            stored = await session.get(RssSubscription, sub.id)
            if not self._same_feed(stored, sub):
                return
            stored.last_success_at = utc_now()
            stored.last_error = None
            stored.failure_count = 0
            stored.next_check_at = utc_now() + timedelta(seconds=stored.interval_seconds)
            if result.not_modified:
                if result.etag is not None:
                    stored.etag = result.etag
                if result.last_modified is not None:
                    stored.last_modified = result.last_modified
                return
            stored.etag = result.etag
            stored.last_modified = result.last_modified
            stored.feed_title = result.feed_title
            selected = {e.entry_key for e in result.entries[-stored.initial_count :]}
            for entry in result.entries:
                # feed_version 隔离更换源后的去重空间，旧表历史保留。
                entry_key = f"{stored.feed_version}:{entry.entry_key}"
                existing = (
                    await session.execute(
                        select(RssEntry.id).where(
                            RssEntry.subscription_id == stored.id,
                            RssEntry.entry_key == entry_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                send = bool(entry.link) and matches_keywords(stored, entry)
                if not stored.initialized:
                    send = (
                        send and stored.initial_policy == "latest" and entry.entry_key in selected
                    )
                row = RssEntry(
                    subscription_id=stored.id,
                    entry_key=entry_key,
                    feed_title=result.feed_title,
                    title=entry.title,
                    link=entry.link,
                    summary=entry.summary,
                    published_at=entry.published_at,
                    status="pending" if send else "skipped",
                )
                try:
                    async with session.begin_nested():
                        session.add(row)
                        await session.flush()
                except IntegrityError:
                    # DB 唯一约束兜底；不意味着多 worker 部署可安全发布。
                    pass
            stored.initialized = True

    async def _publish_pending(self, subscription_id: int) -> None:
        if not getattr(self.publisher, "configured", True):
            return
        async with (
            self.service.subscription_lock(subscription_id),
            self.service.db.session() as session,
        ):
            ids = list(
                (
                    await session.execute(
                        select(RssEntry.id)
                        .where(
                            RssEntry.subscription_id == subscription_id,
                            RssEntry.status == "pending",
                            (RssEntry.next_retry_at.is_(None))
                            | (RssEntry.next_retry_at <= utc_now()),
                        )
                        .order_by(RssEntry.id)
                    )
                ).scalars()
            )
        for entry_id in ids:
            # 外层就绪回调可能等待网络，不持有 CRUD 锁或 bot 全局发送锁。
            if self.can_publish is not None and not await self.can_publish():
                return
            await self._send_one(subscription_id, entry_id)

    async def _send_one(self, subscription_id: int, entry_id: int) -> None:
        async with self._send_lock:
            delay = self._next_send_at - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            if not self.settings.rss_enabled:
                return
            # 翻译在 DB 锁外进行，写回前复查条目仍为 pending；失败不影响发布。
            await self._translate_pending(subscription_id, entry_id)
            # 短锁读条目并原子占用：订阅被删/禁用、entry 被删或不再 pending 时跳过，
            # 不发送、不报错、不复活旧状态。
            async with (
                self.service.subscription_lock(subscription_id),
                self.service.db.session() as session,
            ):
                sub = await session.get(RssSubscription, subscription_id)
                entry = await session.get(RssEntry, entry_id)
                if sub is None or not sub.enabled or not self.settings.rss_enabled:
                    return
                if entry is None or entry.status != "pending":
                    return
                if entry.next_retry_at is not None and entry.next_retry_at > utc_now():
                    return
                if entry.attempts >= self.settings.rss_max_send_attempts:
                    entry.status = "failed"
                    entry.last_error = "RSS 发送已达到重试上限"
                    entry.next_retry_at = None
                    return
                entry.status = "sending"
                entry.attempts += 1
                entry.next_retry_at = None
                # 发送前复查当前启用的目标聊天。
                chat_id = sub.tg_chat_id
            self._next_send_at = (
                asyncio.get_running_loop().time() + self.settings.rss_send_interval_seconds
            )
            # 渲染在锁外进行，耗时计算不阻塞列表、停用、更新和删除。
            try:
                text = render_entry(sub, entry)
            except (RssError, ValueError):
                async with (
                    self.service.subscription_lock(subscription_id),
                    self.service.db.session() as session,
                ):
                    stored = await session.get(RssEntry, entry_id)
                    if not self._same_sending(stored, entry):
                        return
                    stored.status = "failed"
                    stored.last_error = "RSS 消息模板或长度无效"
                    stored.next_retry_at = None
                return
            # 已在途发送无法撤回；网络期间不持有 CRUD 锁或 DB transaction，
            # 取消保留 sending。
            try:
                result = await self.publisher.send(chat_id, text, entry.link)
            except Exception as error:
                await self._record_failure(entry, error)
            else:
                async with (
                    self.service.subscription_lock(subscription_id),
                    self.service.db.session() as session,
                ):
                    stored = await session.get(RssEntry, entry_id)
                    if not self._same_sending(stored, entry):
                        return
                    stored.status = "sent"
                    stored.sent_at = utc_now()
                    stored.tg_message_id = result.message_id
                    stored.tg_chat_id = result.chat_id
                    stored.last_error = None
                    stored.next_retry_at = None

    async def _translate_pending(self, subscription_id: int, entry_id: int) -> None:
        """锁外翻译 title/summary 并短事务写回；失败或条目状态变化则按原文发布。"""
        if not self.settings.rss_translate_enabled:
            return
        async with (
            self.service.subscription_lock(subscription_id),
            self.service.db.session() as session,
        ):
            sub = await session.get(RssSubscription, subscription_id)
            entry = await session.get(RssEntry, entry_id)
            if sub is None or not sub.enabled or not sub.translate:
                return
            if entry is None or entry.status != "pending":
                return
            if entry.next_retry_at is not None and entry.next_retry_at > utc_now():
                return
            # 已缓存译文（含「已是中文」的检测结果）不重复请求。
            if entry.detected_language or entry.translated_title or entry.translated_summary:
                return
            fields = self._translation_fields(entry.title, entry.summary)
        if not fields:
            return
        result = await self._translate_fields(fields)
        if result is None:
            return
        detected, values = result
        async with (
            self.service.subscription_lock(subscription_id),
            self.service.db.session() as session,
        ):
            stored = await session.get(RssEntry, entry_id)
            if stored is None or stored.status != "pending":
                return
            if stored.detected_language or stored.translated_title or stored.translated_summary:
                return
            stored.detected_language = detected
            stored.translated_title = values.get("title")
            stored.translated_summary = values.get("summary")

    @staticmethod
    def _translation_fields(title: str | None, summary: str | None) -> list[tuple[str, str]]:
        fields: list[tuple[str, str]] = []
        if isinstance(title, str) and title.strip():
            fields.append(("title", title))
        if isinstance(summary, str) and summary.strip():
            fields.append(("summary", summary))
        return fields

    async def _translate_fields(
        self, fields: list[tuple[str, str]]
    ) -> tuple[str | None, dict[str, str]] | None:
        """一次批量调用翻译 title/summary；失败返回 None，由调用方按原文发布。"""
        try:
            outcomes = await self.translator.translate(
                [text for _, text in fields], target=self.settings.rss_translate_target
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("RSS 翻译失败，按原文发布")
            return None
        if len(outcomes) != len(fields) or any(outcome.error for outcome in outcomes):
            logger.warning("RSS 翻译失败，按原文发布")
            return None
        detected = next((outcome.language for outcome in outcomes if outcome.language), None)
        values: dict[str, str] = {}
        for (name, _), outcome in zip(fields, outcomes, strict=True):
            if not outcome.skipped and outcome.text:
                values[name] = outcome.text
        return detected, values

    @staticmethod
    def _same_sending(stored: RssEntry | None, snapshot: RssEntry) -> bool:
        return bool(
            stored is not None
            and stored.status == "sending"
            and stored.subscription_id == snapshot.subscription_id
            and stored.entry_key == snapshot.entry_key
            and stored.created_at == snapshot.created_at
            and stored.attempts == snapshot.attempts
        )

    async def _record_failure(self, snapshot: RssEntry, error: Exception) -> None:
        permanent = isinstance(error, BadRequest | Forbidden | RssError)
        retry_after = isinstance(error, RetryAfter)
        transient = isinstance(error, NetworkError | TimeoutError | OSError) or retry_after
        delay = 0.0
        if retry_after:
            raw = error.retry_after
            delay = raw.total_seconds() if isinstance(raw, timedelta) else float(raw)
            if not math.isfinite(delay):
                delay = 60.0
            delay = max(0.0, delay)
            self._next_send_at = max(self._next_send_at, asyncio.get_running_loop().time() + delay)
            defer = getattr(self.publisher, "defer", None)
            if defer is not None:
                defer(delay)
        async with (
            self.service.subscription_lock(snapshot.subscription_id),
            self.service.db.session() as session,
        ):
            entry = await session.get(RssEntry, snapshot.id)
            if not self._same_sending(entry, snapshot):
                return
            exhausted = entry.attempts >= self.settings.rss_max_send_attempts
            # 未知异常也终止，防止程序错误/未知永久失败形成无限发送循环。
            if permanent or not transient or exhausted:
                entry.status = "failed"
                entry.next_retry_at = None
            else:
                entry.status = "pending"
                if not retry_after:
                    delay = min(3600, 5 * 2 ** min(entry.attempts - 1, 10))
                entry.next_retry_at = utc_now() + timedelta(seconds=delay)
            if retry_after:
                entry.last_error = "TG 发送限流，等待重试"
            elif permanent or isinstance(error, TelegramError) and not transient:
                entry.last_error = "TG 永久拒绝发送"
            else:
                entry.last_error = "TG 发送失败或响应未知，重试可能重复"
