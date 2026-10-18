"""刚发出的 Telegram 消息登记表（防回环）。

用户账号发送消息后，Telethon 的 ``events.NewMessage()`` 也会收到自己发出的那条消息。
如果目标会话恰好也被某条规则监听，消息会被再次投递，形成无限循环。这里在发送成功后
登记 ``(chat_id, message_id)``，监听侧在解析前查表跳过。
"""

from __future__ import annotations

import time
from collections import OrderedDict

DEFAULT_TTL_SECONDS = 900.0
DEFAULT_MAX_ENTRIES = 2000
# 发送前登记正文的窗口：只覆盖「更新比 send 返回值先到」这个极短竞态。
DEFAULT_OUTGOING_TTL_SECONDS = 30.0


class SentMessageRegistry:
    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        outgoing_ttl_seconds: float = DEFAULT_OUTGOING_TTL_SECONDS,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max(1, max_entries)
        self._outgoing_ttl_seconds = outgoing_ttl_seconds
        self._entries: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._outgoing: OrderedDict[str, float] = OrderedDict()

    @staticmethod
    def _key(chat_id: object, message_id: object) -> tuple[str, str] | None:
        if chat_id is None or message_id is None:
            return None
        chat = str(chat_id).strip()
        message = str(message_id).strip()
        if not chat or not message:
            return None
        return chat, message

    def remember(self, chat_id: object, message_id: object) -> None:
        key = self._key(chat_id, message_id)
        if key is None:
            return
        now = time.monotonic()
        self._prune(now)
        self._entries[key] = now
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def is_recent(self, chat_id: object, message_id: object) -> bool:
        key = self._key(chat_id, message_id)
        if key is None:
            return False
        seen_at = self._entries.get(key)
        if seen_at is None:
            return False
        if time.monotonic() - seen_at > self._ttl_seconds:
            self._entries.pop(key, None)
            return False
        return True

    def _prune(self, now: float) -> None:
        while self._entries:
            oldest_key, seen_at = next(iter(self._entries.items()))
            if now - seen_at <= self._ttl_seconds and len(self._entries) < self._max_entries:
                return
            self._entries.pop(oldest_key, None)

    def clear(self) -> None:
        self._entries.clear()
        self._outgoing.clear()

    # -------------------------------------------------- 发送前的正文登记

    def mark_outgoing(self, text: str | None) -> None:
        """发送前登记正文。

        Telethon 可能在我们拿到 ``send_message`` 返回值之前就把这条消息的更新推给
        监听器，此时还查不到 ``message_id``；用「正文 + 短 TTL + 命中即消费」兜住
        这个竞态，避免消息被自己重新抓取后无限循环。
        """
        key = text or ""
        now = time.monotonic()
        self._prune_outgoing(now)
        self._outgoing[key] = now
        self._outgoing.move_to_end(key)
        while len(self._outgoing) > self._max_entries:
            self._outgoing.popitem(last=False)

    def consume_outgoing(self, text: str | None) -> bool:
        """命中则删除该条登记，保证一条登记最多只吞掉一条消息。"""
        key = text or ""
        seen_at = self._outgoing.pop(key, None)
        if seen_at is None:
            return False
        return time.monotonic() - seen_at <= self._outgoing_ttl_seconds

    def _prune_outgoing(self, now: float) -> None:
        while self._outgoing:
            oldest_key, seen_at = next(iter(self._outgoing.items()))
            if (
                now - seen_at <= self._outgoing_ttl_seconds
                and len(self._outgoing) < self._max_entries
            ):
                return
            self._outgoing.pop(oldest_key, None)
