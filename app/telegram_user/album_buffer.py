from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from app.rules.models import TelegramForwardMessage, TelegramLink

logger = logging.getLogger(__name__)

MessageCallback = Callable[[TelegramForwardMessage], Awaitable[None]]


class TelegramAlbumBuffer:
    def __init__(self, delay_seconds: float, on_message: MessageCallback) -> None:
        self.delay_seconds = delay_seconds
        self.on_message = on_message
        self._groups: dict[tuple[str | None, int | None, int], list[TelegramForwardMessage]] = {}
        self._tasks: dict[tuple[str | None, int | None, int], asyncio.Task] = {}

    async def handle(self, message: TelegramForwardMessage) -> None:
        if not message.grouped_id or self.delay_seconds <= 0:
            await self.on_message(message)
            return

        key = (message.account_id, message.chat_id, message.grouped_id)
        self._groups.setdefault(key, []).append(message)
        task = self._tasks.get(key)
        if task and not task.done():
            task.cancel()
        self._tasks[key] = asyncio.create_task(self._flush_later(key), name=f"tg-album-{key}")

    async def flush_all(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        keys = list(self._groups.keys())
        for key in keys:
            await self._flush(key)

    async def _flush_later(self, key: tuple[str | None, int | None, int]) -> None:
        try:
            await asyncio.sleep(self.delay_seconds)
            await self._flush(key)
        except asyncio.CancelledError:
            pass

    async def _flush(self, key: tuple[str | None, int | None, int]) -> None:
        messages = self._groups.pop(key, [])
        self._tasks.pop(key, None)
        if not messages:
            return
        messages.sort(key=lambda item: item.message_id)
        merged = self._merge(messages)
        logger.info(
            "Merged Telegram album account=%s grouped_id=%s chat=%s messages=%s media=%s",
            merged.account_id,
            merged.grouped_id,
            merged.chat_id,
            len(messages),
            len(merged.media_paths),
        )
        await self.on_message(merged)

    @staticmethod
    def _merge(messages: list[TelegramForwardMessage]) -> TelegramForwardMessage:
        base = next((message for message in messages if message.text.strip()), messages[0])
        text_parts: list[str] = []
        links: list[TelegramLink] = []
        media_paths: list[Path] = []
        media_types: list[str] = []
        seen_links: set[tuple[str, str]] = set()
        preview_message = next(
            (
                message
                for message in messages
                if message.webpage_title or message.webpage_description or message.webpage_url
            ),
            None,
        )
        webpage_title = preview_message.webpage_title if preview_message else None
        webpage_description = preview_message.webpage_description if preview_message else None
        webpage_url = preview_message.webpage_url if preview_message else None

        for message in messages:
            raw_text = message.text or ""
            text = raw_text.strip()
            text_offset: int | None = None
            leading_trim = len(raw_text) - len(raw_text.lstrip())
            if text and text not in text_parts:
                text_offset = len("\n".join(text_parts))
                if text_parts:
                    text_offset += 1
                text_parts.append(text)
            for link in message.links:
                key = (link.text, link.url)
                if key in seen_links:
                    continue
                seen_links.add(key)
                adjusted_start = None
                adjusted_end = None
                if (
                    text_offset is not None
                    and link.text_start is not None
                    and link.text_end is not None
                ):
                    local_start = link.text_start - leading_trim
                    local_end = link.text_end - leading_trim
                    if 0 <= local_start < local_end <= len(text):
                        adjusted_start = text_offset + local_start
                        adjusted_end = text_offset + local_end
                links.append(
                    TelegramLink(
                        text=link.text,
                        url=link.url,
                        source=link.source,
                        text_start=adjusted_start,
                        text_end=adjusted_end,
                    )
                )
            media_paths.extend(message.media_paths)
            media_types.extend(message.media_types)

        return replace(
            base,
            message_id=messages[0].message_id,
            text="\n".join(text_parts),
            media_path=media_paths[0] if media_paths else None,
            media_type=media_types[0] if media_types else None,
            raw_url=webpage_url or base.raw_url,
            webpage_title=webpage_title,
            webpage_description=webpage_description,
            webpage_url=webpage_url,
            links=links,
            media_paths=media_paths,
            media_types=media_types,
        )
