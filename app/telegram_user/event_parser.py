from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from telethon import events
from telethon.tl.types import Channel, Chat, MessageEntityTextUrl, MessageEntityUrl, User

from app.rules.models import (
    TelegramForwardMessage,
    TelegramLink,
    normalize_telegram_url,
    url_dedupe_key,
)
from app.telegram_user.media_downloader import is_link_preview_media

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WebpagePreview:
    title: str | None = None
    description: str | None = None
    url: str | None = None


def _display_name(sender: Any) -> str | None:
    if sender is None:
        return None
    first_name = getattr(sender, "first_name", None) or ""
    last_name = getattr(sender, "last_name", None) or ""
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if full_name:
        return full_name
    return getattr(sender, "title", None) or getattr(sender, "username", None)


def _chat_title(chat: Any) -> str | None:
    if chat is None:
        return None
    return getattr(chat, "title", None) or getattr(chat, "username", None) or _display_name(chat)


def _chat_type(event: events.NewMessage.Event, chat: Any) -> str:
    if event.is_private:
        return "private"
    if event.is_group:
        return "group"
    if event.is_channel:
        return "channel"
    if isinstance(chat, Channel):
        return "channel" if not getattr(chat, "megagroup", False) else "group"
    if isinstance(chat, Chat):
        return "group"
    return "unknown"


def _media_type(
    event: events.NewMessage.Event,
    *,
    include_link_preview_media: bool = False,
) -> str | None:
    message = event.message
    if not message or not message.media:
        return None
    if is_link_preview_media(message):
        return "link_preview" if include_link_preview_media else None
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "document", None):
        return "document"
    return "media"


def _normalize_url(url: str) -> str:
    return normalize_telegram_url(url)


def _clean_preview_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _webpage_from_message(message: Any) -> Any | None:
    media = getattr(message, "media", None)
    webpage = getattr(media, "webpage", None)
    if webpage is not None:
        return webpage
    return getattr(message, "web_preview", None)


def _extract_webpage_preview(message: Any) -> WebpagePreview:
    webpage = _webpage_from_message(message)
    if webpage is None:
        return WebpagePreview()

    title = _clean_preview_text(getattr(webpage, "title", None))
    description = _clean_preview_text(getattr(webpage, "description", None))
    raw_url = _clean_preview_text(getattr(webpage, "url", None)) or _clean_preview_text(
        getattr(webpage, "display_url", None)
    )
    url = _normalize_url(raw_url) if raw_url else None
    return WebpagePreview(title=title, description=description, url=url)


def _extract_entity_links(message: Any) -> list[TelegramLink]:
    links: list[TelegramLink] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        entities_text = message.get_entities_text()
    except Exception:
        logger.debug("Failed to read Telegram message entities", exc_info=True)
        return []

    for entity, text in entities_text:
        url: str | None = None
        source: str | None = None
        if isinstance(entity, MessageEntityTextUrl):
            url = entity.url
            source = "text_url"
        elif isinstance(entity, MessageEntityUrl):
            url = text
            source = "visible_url"
        if not url or not source:
            continue
        url = _normalize_url(url)
        text = (text or url).strip()
        start = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        end = start + length if isinstance(start, int) and isinstance(length, int) else None
        key = (source, text, url)
        if key in seen:
            continue
        seen.add(key)
        links.append(
            TelegramLink(
                text=text,
                url=url,
                source=source,
                text_start=start if isinstance(start, int) else None,
                text_end=end,
            )
        )
    return links


def _button_rows(buttons: Any) -> Iterable[Any]:
    if not buttons:
        return []
    if isinstance(buttons, list | tuple):
        return buttons
    return [buttons]


def _buttons_from_reply_markup(message: Any) -> Iterator[Any]:
    reply_markup = getattr(message, "reply_markup", None)
    for row in getattr(reply_markup, "rows", None) or []:
        yield getattr(row, "buttons", []) or []


def _button_url(button: Any) -> str | None:
    url = getattr(button, "url", None)
    if url:
        return str(url)
    raw_button = getattr(button, "button", None)
    url = getattr(raw_button, "url", None)
    if url:
        return str(url)
    return None


def _button_text(button: Any, url: str) -> str:
    text = getattr(button, "text", None)
    if text:
        return str(text).strip()
    raw_button = getattr(button, "button", None)
    text = getattr(raw_button, "text", None)
    if text:
        return str(text).strip()
    return url


def _iter_url_buttons(message: Any) -> Iterator[tuple[str, str]]:
    high_level_buttons = getattr(message, "buttons", None)
    rows = (
        list(_button_rows(high_level_buttons))
        if high_level_buttons
        else list(_buttons_from_reply_markup(message))
    )
    for row in rows:
        row_buttons = row if isinstance(row, list | tuple) else [row]
        for button in row_buttons:
            url = _button_url(button)
            if not url:
                continue
            normalized_url = _normalize_url(url)
            if not normalized_url:
                continue
            yield _button_text(button, normalized_url), normalized_url


def _extract_button_links(message: Any) -> list[TelegramLink]:
    links: list[TelegramLink] = []
    seen: set[str] = set()
    for text, url in _iter_url_buttons(message):
        key = url_dedupe_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        links.append(TelegramLink(text=text, url=url, source="button_url"))
    return links


def _extract_links(event: events.NewMessage.Event) -> list[TelegramLink]:
    message = event.message
    if not message:
        return []
    return [*_extract_entity_links(message), *_extract_button_links(message)]


async def parse_event(
    event: events.NewMessage.Event,
    *,
    media_path: Path | None = None,
    include_link_preview_media: bool = False,
) -> TelegramForwardMessage:
    chat = await event.get_chat()
    sender = await event.get_sender()
    sender_id = getattr(sender, "id", None) or event.sender_id
    sender_username = getattr(sender, "username", None)
    sender_is_bot = bool(getattr(sender, "bot", False)) if isinstance(sender, User) else False
    media_type = _media_type(event, include_link_preview_media=include_link_preview_media)
    webpage_preview = _extract_webpage_preview(event.message)

    return TelegramForwardMessage(
        message_id=event.message.id,
        chat_id=event.chat_id,
        chat_title=_chat_title(chat),
        chat_type=_chat_type(event, chat),
        sender_id=sender_id,
        sender_username=sender_username,
        sender_display_name=_display_name(sender),
        sender_is_bot=sender_is_bot,
        text=event.raw_text or "",
        media_type=media_type,
        media_path=media_path,
        date=getattr(event.message, "date", None) or datetime.now(),
        raw_url=webpage_preview.url,
        webpage_title=webpage_preview.title,
        webpage_description=webpage_preview.description,
        webpage_url=webpage_preview.url,
        links=_extract_links(event),
        grouped_id=getattr(event.message, "grouped_id", None),
        media_paths=[media_path] if media_path else [],
        media_types=[media_type] if media_type else [],
    )
