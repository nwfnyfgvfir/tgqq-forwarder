from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from telethon import events
from telethon.tl.types import Channel, Chat, User

from app.rules.models import TelegramForwardMessage

logger = logging.getLogger(__name__)


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


def _media_type(event: events.NewMessage.Event) -> str | None:
    message = event.message
    if not message or not message.media:
        return None
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.document:
        return "document"
    return "media"


async def parse_event(
    event: events.NewMessage.Event,
    *,
    media_path: Path | None = None,
) -> TelegramForwardMessage:
    chat = await event.get_chat()
    sender = await event.get_sender()
    sender_id = getattr(sender, "id", None) or event.sender_id
    sender_username = getattr(sender, "username", None)
    sender_is_bot = bool(getattr(sender, "bot", False)) if isinstance(sender, User) else False

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
        media_type=_media_type(event),
        media_path=media_path,
        date=getattr(event.message, "date", None) or datetime.now(),
    )
