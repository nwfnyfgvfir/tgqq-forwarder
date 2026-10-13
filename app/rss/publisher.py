from __future__ import annotations

import asyncio
import hashlib
import weakref
from dataclasses import dataclass

from telegram import Bot, LinkPreviewOptions
from telegram.error import RetryAfter
from telegram.request import HTTPXRequest

from app.config import Settings
from app.rss.errors import RssError
from app.rss.formatting import fit_message


@dataclass(slots=True)
class PublishResult:
    message_id: int
    chat_id: int


class _BotGate:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.next_send_at = 0.0


# 同一 event loop、同一 bot 的多个 RSS publisher 共享限流，不保存原始 token。
_GATES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class RssPublisher:
    def __init__(self, settings: Settings, sender=None) -> None:
        self.settings = settings
        self._token = settings.rss_bot_token or settings.tg_admin_bot_token
        self._bot = sender
        self._initialized = False

    @property
    def configured(self) -> bool:
        return bool(self._token or self._bot)

    def _gate(self) -> _BotGate:
        loop = asyncio.get_running_loop()
        gates = _GATES.setdefault(loop, {})
        identity = self._token or f"fake:{id(self._bot)}"
        key = hashlib.sha256(identity.encode()).hexdigest()
        return gates.setdefault(key, _BotGate())

    def defer(self, seconds: float) -> None:
        gate = self._gate()
        gate.next_send_at = max(gate.next_send_at, asyncio.get_running_loop().time() + seconds)

    async def send(self, tg_chat_id: str, text: str, link: str = "") -> PublishResult:
        if not self.configured:
            raise RssError("rss_publisher_missing", 409, "RSS 发布 Bot 尚未配置")
        gate = self._gate()
        async with gate.lock:
            loop = asyncio.get_running_loop()
            delay = gate.next_send_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._bot is None:
                request = HTTPXRequest(
                    connect_timeout=self.settings.tg_admin_bot_connect_timeout,
                    read_timeout=self.settings.tg_admin_bot_request_timeout,
                    write_timeout=self.settings.tg_admin_bot_request_timeout,
                    pool_timeout=self.settings.tg_admin_bot_pool_timeout,
                    httpx_kwargs={"trust_env": False},
                )
                self._bot = Bot(token=self._token, request=request)
            if not self._initialized:
                await self._bot.initialize()
                self._initialized = True
            gate.next_send_at = loop.time() + self.settings.rss_send_interval_seconds
            try:
                message = await self._bot.send_message(
                    chat_id=int(tg_chat_id) if tg_chat_id.startswith("-") else tg_chat_id,
                    text=fit_message(text, link),
                    parse_mode=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except RetryAfter as error:
                raw = error.retry_after
                seconds = raw.total_seconds() if hasattr(raw, "total_seconds") else float(raw)
                # 释放共享发送锁前设置 bot 级暂停，避免另一订阅提前发送。
                self.defer(max(0.0, seconds))
                raise
            return PublishResult(message.message_id, int(message.chat.id))

    async def stop(self) -> None:
        if self._bot is not None and self._initialized:
            await self._bot.shutdown()
            self._initialized = False
