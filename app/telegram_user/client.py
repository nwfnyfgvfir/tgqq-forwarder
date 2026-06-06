from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, events

from app.config import Settings
from app.rules.models import TelegramForwardMessage
from app.telegram_user.album_buffer import TelegramAlbumBuffer
from app.telegram_user.dialog_cache import DialogCache
from app.telegram_user.event_parser import parse_event
from app.telegram_user.media_downloader import TelegramMediaDownloader

logger = logging.getLogger(__name__)

MessageCallback = Callable[[TelegramForwardMessage], Awaitable[None]]


class TelegramUserListener:
    def __init__(self, settings: Settings, on_message: MessageCallback) -> None:
        self.settings = settings
        self.on_message = on_message
        self.client = TelegramClient(
            str(settings.telegram_session_path),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        self.downloader = TelegramMediaDownloader(
            settings.media_dir,
            enabled=settings.telegram_download_media,
            max_media_mb=settings.telegram_max_media_mb,
        )
        self.album_buffer = TelegramAlbumBuffer(
            settings.telegram_album_buffer_seconds,
            on_message,
        )
        self.dialogs = DialogCache(self.client)
        self._started = False

    @property
    def is_connected(self) -> bool:
        return bool(self.client.is_connected())

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram user session is not authorized. Run: "
                "python -m app.telegram_user.login"
            )

        self.client.add_event_handler(self._handle_new_message, events.NewMessage())
        self._started = True
        me = await self.client.get_me()
        logger.info("Telegram user listener started as %s (%s)", getattr(me, "username", None), me.id)

    async def stop(self) -> None:
        if self._started:
            self.client.remove_event_handler(self._handle_new_message)
        await self.album_buffer.flush_all()
        await self.client.disconnect()
        logger.info("Telegram user listener stopped")

    async def wait_disconnected(self) -> None:
        await self.client.disconnected

    async def _handle_new_message(self, event: events.NewMessage.Event) -> None:
        try:
            media_path = await self.downloader.download(event)
            message = await parse_event(event, media_path=media_path)
            await self.album_buffer.handle(message)
        except Exception:
            logger.exception("Failed to handle Telegram message")
