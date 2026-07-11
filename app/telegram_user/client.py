from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, events

from app.config import Settings, TelegramAccountConfig
from app.rules.models import TelegramForwardMessage
from app.telegram_user.album_buffer import TelegramAlbumBuffer
from app.telegram_user.dialog_cache import DialogCache
from app.telegram_user.event_parser import parse_event
from app.telegram_user.media_downloader import TelegramMediaDownloader

logger = logging.getLogger(__name__)

MessageCallback = Callable[[TelegramForwardMessage], Awaitable[None]]


class TelegramUserListener:
    def __init__(
        self,
        settings: Settings,
        on_message: MessageCallback,
        *,
        account: TelegramAccountConfig | None = None,
    ) -> None:
        self.settings = settings
        self.on_message = on_message
        self.account = account or settings.enabled_telegram_accounts()[0]
        api_id = self.account.resolved_api_id(settings.telegram_api_id)
        api_hash = self.account.resolved_api_hash(settings.telegram_api_hash)
        self.client = TelegramClient(
            str(self.account.session_path),
            api_id,
            api_hash,
        )
        self.downloader = TelegramMediaDownloader(
            settings.media_dir,
            enabled=settings.telegram_download_media,
            max_media_mb=settings.telegram_max_media_mb,
            download_link_preview_media=settings.telegram_forward_link_preview_media,
            account_id=self.account.id,
        )
        self.album_buffer = TelegramAlbumBuffer(
            settings.telegram_album_buffer_seconds,
            on_message,
        )
        self.dialogs = DialogCache(self.client)
        self._started = False
        self.account_user_id: int | None = None
        self.account_username: str | None = None
        self.is_authorized = False

    @property
    def account_id(self) -> str:
        return self.account.id

    @property
    def is_connected(self) -> bool:
        return bool(self.client.is_connected())

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                f"Telegram user session for account '{self.account.id}' is not authorized. "
                f"Run: python -m app.telegram_user.login --account {self.account.id}"
            )

        self.client.add_event_handler(self._handle_new_message, events.NewMessage())
        self._started = True
        self.is_authorized = True
        me = await self.client.get_me()
        self.account_user_id = getattr(me, "id", None)
        self.account_username = getattr(me, "username", None)
        logger.info(
            "Telegram user listener started account=%s as %s (%s)",
            self.account.id,
            self.account_username,
            self.account_user_id,
        )

    async def stop(self) -> None:
        if self._started:
            self.client.remove_event_handler(self._handle_new_message)
        await self.album_buffer.flush_all()
        await self.client.disconnect()
        self._started = False
        self.is_authorized = False
        logger.info("Telegram user listener stopped account=%s", self.account.id)

    async def wait_disconnected(self) -> None:
        await self.client.disconnected

    async def _handle_new_message(self, event: events.NewMessage.Event) -> None:
        try:
            media_path = await self.downloader.download(event)
            message = await parse_event(
                event,
                media_path=media_path,
                include_link_preview_media=self.settings.telegram_forward_link_preview_media,
            )
            message.account_id = self.account.id
            message.account_user_id = self.account_user_id
            message.account_username = self.account_username
            await self.album_buffer.handle(message)
        except Exception:
            logger.exception(
                "Failed to handle Telegram message account=%s",
                self.account.id,
            )
