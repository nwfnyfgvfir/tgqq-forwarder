from __future__ import annotations

import asyncio
import logging
from collections import Counter
from types import SimpleNamespace

from sqlalchemy import delete, func, select, update

from app.config import Settings
from app.rss.errors import RssError
from app.rss.fetcher import RssFetcher
from app.rss.formatting import matches_keywords, render_entry
from app.rss.schemas import SubscriptionInput
from app.rss.translator import AITranslator
from app.storage.db import Database
from app.storage.models import RssEntry, RssSubscription, utc_now

logger = logging.getLogger(__name__)


class RssService:
    """CRUD 与 worker 共用订阅锁；仅支持一个进程内的单实例 worker。"""

    def __init__(self, db: Database, settings: Settings, fetcher=None, translator=None) -> None:
        self.db = db
        self.settings = settings
        self.fetcher = fetcher if fetcher is not None else RssFetcher(settings)
        self.translator = translator if translator is not None else AITranslator(settings)
        self._locks: dict[int, asyncio.Lock] = {}

    def subscription_lock(self, subscription_id: int) -> asyncio.Lock:
        # 删除后不移除锁，避免持有旧锁的排队任务与新锁发生竞争。
        return self._locks.setdefault(subscription_id, asyncio.Lock())

    @staticmethod
    async def _get(session, subscription_id: int) -> RssSubscription:
        sub = await session.get(RssSubscription, subscription_id)
        if sub is None:
            raise RssError("rss_not_found", 404, "RSS 订阅不存在")
        return sub

    @staticmethod
    async def _serialize(session, sub: RssSubscription) -> dict:
        rows = (
            await session.execute(
                select(RssEntry.status, func.count())
                .where(RssEntry.subscription_id == sub.id)
                .group_by(RssEntry.status)
            )
        ).all()
        counts = dict(rows)
        return {
            **sub.model_dump(),
            "sent_count": counts.get("sent", 0),
            "pending_count": counts.get("pending", 0) + counts.get("sending", 0),
            "failed_count": counts.get("failed", 0),
        }

    async def list_subscriptions(self) -> list[dict]:
        async with self.db.session() as session:
            ids = await session.execute(select(RssSubscription.id).order_by(RssSubscription.id))
            ids = list(ids.scalars())
        items = []
        for subscription_id in ids:
            try:
                items.append(await self.get_subscription(subscription_id))
            except RssError as error:
                if error.code != "rss_not_found":
                    raise
        return items

    async def create_subscription(self, payload: SubscriptionInput) -> dict:
        sub = RssSubscription(**payload.model_dump())
        async with self.db.session() as session:
            session.add(sub)
            await session.flush()
            result = await self._serialize(session, sub)
        return result

    async def get_subscription(self, subscription_id: int) -> dict:
        async with self.subscription_lock(subscription_id), self.db.session() as session:
            return await self._serialize(session, await self._get(session, subscription_id))

    async def update_subscription(self, subscription_id: int, payload: SubscriptionInput) -> dict:
        async with self.subscription_lock(subscription_id), self.db.session() as session:
            sub = await self._get(session, subscription_id)
            if sub.feed_url != payload.feed_url:
                # 保留历史 sent 与去重记录；旧源未完成条目终止，不会错发到新源。
                await session.execute(
                    update(RssEntry)
                    .where(
                        RssEntry.subscription_id == subscription_id,
                        RssEntry.status.in_(["pending", "sending", "failed"]),
                    )
                    .values(status="skipped", next_retry_at=None, last_error="订阅源已更换")
                )
                sub.feed_version += 1
                sub.initialized = False
                sub.etag = None
                sub.last_modified = None
                sub.feed_title = ""
                sub.last_checked_at = None
                sub.last_success_at = None
                sub.last_error = None
                sub.failure_count = 0
            # 目标修改影响之后的 pending；已在途发送不能撤回，sent 不重发。
            for key, value in payload.model_dump().items():
                setattr(sub, key, value)
            sub.updated_at = utc_now()
            sub.next_check_at = None
            await session.flush()
            return await self._serialize(session, sub)

    async def set_enabled(self, subscription_id: int, enabled: bool) -> dict:
        async with self.subscription_lock(subscription_id), self.db.session() as session:
            sub = await self._get(session, subscription_id)
            sub.enabled = enabled
            sub.updated_at = utc_now()
            if enabled:
                sub.next_check_at = None
            await session.flush()
            return await self._serialize(session, sub)

    async def delete_subscription(self, subscription_id: int) -> bool:
        async with self.subscription_lock(subscription_id), self.db.session() as session:
            await self._get(session, subscription_id)
            await session.execute(
                delete(RssEntry).where(RssEntry.subscription_id == subscription_id)
            )
            await session.execute(
                delete(RssSubscription).where(RssSubscription.id == subscription_id)
            )
        return True

    async def list_entries(self, subscription_id: int, limit: int = 50) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise RssError("rss_invalid_limit", 422, "条目数量必须在 1 到 500 之间")
        async with self.subscription_lock(subscription_id), self.db.session() as session:
            await self._get(session, subscription_id)
            entries = (
                (
                    await session.execute(
                        select(RssEntry)
                        .where(RssEntry.subscription_id == subscription_id)
                        .order_by(RssEntry.id.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [entry.model_dump() for entry in entries]

    async def preview(self, payload: SubscriptionInput) -> dict:
        try:
            result = await self.fetcher.fetch(payload.feed_url)
        except RssError:
            raise
        except Exception:
            raise RssError("rss_fetch_failed", 502, "订阅预览抓取失败") from None
        warnings = list(result.warnings)
        if len(result.entries) > 10:
            warnings.append("预览仅展示最近 10 条，已截断其余条目")
        selected = result.entries[-10:]
        translations: dict[int, dict] = {}
        if payload.translate and self.settings.rss_translate_enabled and selected:
            translations = await self._translate_preview(selected, warnings)
        entries = []
        for index, entry in enumerate(selected):
            translation = translations.get(index, {})
            target = self._with_translation(entry, translation)
            try:
                rendered = render_entry(payload, target, result.feed_title)
            except RssError:
                rendered = ""
                warnings.append("部分原文链接超过消息长度限制，将跳过发布")
            entries.append(
                {
                    "title": entry.title,
                    "link": entry.link,
                    "summary": entry.summary,
                    "published_at": entry.published_at,
                    "translated_title": translation.get("translated_title"),
                    "translated_summary": translation.get("translated_summary"),
                    "detected_language": translation.get("detected_language"),
                    "rendered_text": rendered,
                    "matches": matches_keywords(payload, entry),
                }
            )
        return {
            "feed_title": result.feed_title,
            "entries": entries,
            "warnings": list(dict.fromkeys(warnings)),
        }

    @staticmethod
    def _with_translation(entry, translation: dict):
        # FeedEntry 是 slots dataclass，用 SimpleNamespace 包装以承载译文字段。
        return SimpleNamespace(
            title=entry.title,
            summary=entry.summary,
            link=entry.link,
            published_at=entry.published_at,
            feed_title=getattr(entry, "feed_title", ""),
            translated_title=translation.get("translated_title"),
            translated_summary=translation.get("translated_summary"),
            detected_language=translation.get("detected_language"),
        )

    async def _translate_preview(self, entries: list, warnings: list[str]) -> dict[int, dict]:
        """批量翻译预览条目（最多 10 条）；失败时提示按原文展示，不写库。"""
        texts: list[str] = []
        index: list[tuple[int, str]] = []
        for position, entry in enumerate(entries):
            if entry.title.strip():
                texts.append(entry.title)
                index.append((position, "title"))
            if entry.summary.strip():
                texts.append(entry.summary)
                index.append((position, "summary"))
        if not texts:
            return {}
        try:
            outcomes = await self.translator.translate(
                texts, target=self.settings.rss_translate_target
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            warnings.append("部分条目翻译失败，预览按原文展示")
            return {}
        if len(outcomes) != len(texts) or any(outcome.error for outcome in outcomes):
            warnings.append("部分条目翻译失败，预览按原文展示")
            return {}
        result: dict[int, dict] = {}
        for (position, field), outcome in zip(index, outcomes, strict=True):
            bucket = result.setdefault(
                position,
                {"detected_language": None, "translated_title": None, "translated_summary": None},
            )
            if outcome.language and bucket["detected_language"] is None:
                bucket["detected_language"] = outcome.language
            if not outcome.skipped and outcome.text:
                bucket[f"translated_{field}"] = outcome.text
        return result

    async def status(self) -> dict:
        async with self.db.session() as session:
            total, enabled = (
                await session.execute(
                    select(
                        func.count(RssSubscription.id),
                        func.count(RssSubscription.id).filter(RssSubscription.enabled.is_(True)),
                    )
                )
            ).one()
            rows = (
                await session.execute(
                    select(RssEntry.status, func.count()).group_by(RssEntry.status)
                )
            ).all()
            counts = Counter(dict(rows))
        return {
            "enabled": self.settings.rss_enabled,
            "publisher_configured": bool(
                self.settings.rss_bot_token or self.settings.tg_admin_bot_token
            ),
            "total_subscriptions": total,
            "enabled_subscriptions": enabled,
            "pending_count": counts["pending"] + counts["sending"],
            "failed_count": counts["failed"],
            "sent_count": counts["sent"],
        }
