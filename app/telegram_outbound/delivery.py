"""把消息投递到 Telegram 会话。

两种发送身份，由 ``ForwardTarget.target_type`` 决定：

* ``chat``：用已登录的 Telethon 用户账号发送。可以发到该账号所在的任意群 / 频道 / 私聊，
  也能发给自己（``target_id`` 写 ``me`` 即收藏夹）。``account_id`` 为空时用默认账号。
* ``bot``：用 Bot API 发送，Bot 必须已经在目标会话里（频道 / 群），无法主动私聊陌生人。

发送成功后会把 ``(chat_id, message_id)`` 记进 :class:`SentMessageRegistry`，
监听侧据此跳过自己发出的消息，避免「TG → TG」自激回环。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram import Bot, LinkPreviewOptions
from telegram.request import HTTPXRequest

from app.config import Settings
from app.platforms import TELEGRAM_TARGET_BOT, TELEGRAM_TARGET_CHAT
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardTarget
from app.telegram_outbound.registry import SentMessageRegistry
from app.telegram_user.accounts import TelegramAccountManager

logger = logging.getLogger(__name__)

SAVED_MESSAGE_ALIASES = {"me", "self", "saved", "saved_messages", "savedmessages"}
_NUMERIC_ID_RE = re.compile(r"^-?\d+$")


class TelegramDeliveryError(Exception):
    """Telegram 目标不可用或发送失败。"""


def _existing_media(message: TelegramForwardMessage) -> list[Path]:
    paths: list[Path] = []
    for path in message.media_paths or []:
        if path and Path(path).exists():
            paths.append(Path(path))
    return paths


def _media_kind(message: TelegramForwardMessage, index: int) -> str | None:
    types = message.media_types or []
    if index < len(types):
        return types[index]
    return message.media_type


def _user_chat_ref(target_id: str) -> object:
    """用户账号可用 ``me`` / 数字 ID / @username / 邀请链接发送。"""
    text = (target_id or "").strip()
    if not text:
        raise TelegramDeliveryError("Telegram 目标会话为空")
    if text.lower() in SAVED_MESSAGE_ALIASES:
        return "me"
    if _NUMERIC_ID_RE.match(text):
        return int(text)
    return text


def _bot_chat_ref(target_id: str) -> int | str:
    """Bot API 只接受数字 chat_id 或 @channelusername。"""
    text = (target_id or "").strip()
    if not text:
        raise TelegramDeliveryError("Telegram 目标会话为空")
    if text.lower() in SAVED_MESSAGE_ALIASES:
        raise TelegramDeliveryError("Bot 无法发送到收藏夹，请改用用户账号或填写具体 chat_id")
    if _NUMERIC_ID_RE.match(text):
        return int(text)
    return text


class TelegramUserSender:
    """用 Telethon 用户账号发送。"""

    def __init__(
        self,
        settings: Settings,
        account_manager_getter: Callable[[], TelegramAccountManager | None],
        registry: SentMessageRegistry,
    ) -> None:
        self.settings = settings
        self.account_manager_getter = account_manager_getter
        self.registry = registry

    @property
    def enabled(self) -> bool:
        manager = self.account_manager_getter()
        return bool(manager and manager.is_any_connected())

    async def send(
        self,
        target: ForwardTarget,
        message: TelegramForwardMessage,
        text: str,
    ) -> None:
        manager = self.account_manager_getter()
        if manager is None:
            raise TelegramDeliveryError("Telegram 用户监听器尚未就绪，无法用账号发送")
        account_id = (target.account_id or "").strip() or None
        listener = manager.get(account_id)
        if listener is None:
            raise TelegramDeliveryError(f"Telegram 账号未启动：{account_id or '默认账号'}")
        if not listener.is_authorized:
            raise TelegramDeliveryError(f"Telegram 账号未登录：{account_id or listener.account_id}")

        client = listener.client
        entity = _user_chat_ref(target.target_id)
        # 先登记正文：更新有可能早于 send 的返回值到达监听器。
        self.registry.mark_outgoing(text)
        media = _existing_media(message)
        if media:
            sent = await client.send_file(
                entity,
                str(media[0]),
                caption=text or None,
                parse_mode=None,
            )
            self._remember(sent)
            for path in media[1:]:
                sent = await client.send_file(entity, str(path), parse_mode=None)
                self._remember(sent)
            return
        sent = await client.send_message(entity, text, parse_mode=None, link_preview=False)
        self._remember(sent)

    def _remember(self, sent: Any) -> None:
        for item in sent if isinstance(sent, list | tuple) else [sent]:
            self.registry.remember(
                getattr(item, "chat_id", None),
                getattr(item, "id", None),
            )


class TelegramBotSender:
    """用 Bot API 发送（token 优先 ``TELEGRAM_SEND_BOT_TOKEN``）。"""

    def __init__(
        self,
        settings: Settings,
        registry: SentMessageRegistry,
        *,
        bot: Bot | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self._token = (
            settings.telegram_send_bot_token
            or settings.rss_bot_token
            or settings.tg_admin_bot_token
        )
        self._bot = bot
        self._initialized = False
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._token or self._bot)

    async def send(
        self,
        target: ForwardTarget,
        message: TelegramForwardMessage,
        text: str,
    ) -> None:
        if not self.configured:
            raise TelegramDeliveryError(
                "Telegram Bot 发送未配置：请设置 TELEGRAM_SEND_BOT_TOKEN"
                "（或复用 RSS_BOT_TOKEN / TG_ADMIN_BOT_TOKEN）"
            )
        bot = await self._ensure_bot()
        chat_id = _bot_chat_ref(target.target_id)
        media = _existing_media(message)
        if media:
            sent = await self._send_media(bot, chat_id, media[0], _media_kind(message, 0), text)
            self._remember(sent)
            for index, path in enumerate(media[1:], start=1):
                sent = await self._send_media(bot, chat_id, path, _media_kind(message, index), "")
                self._remember(sent)
            return
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text or "[空消息]",
            parse_mode=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        self._remember(sent)

    async def _send_media(
        self,
        bot: Bot,
        chat_id: int | str,
        path: Path,
        kind: str | None,
        caption: str,
    ) -> Any:
        kwargs: dict[str, Any] = {"chat_id": chat_id, "caption": caption or None}
        suffix = path.suffix.lower()
        if kind == "photo" or suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            method = bot.send_photo
            kwargs["photo"] = path.open("rb")
        elif kind == "video" or suffix in {".mp4", ".mov", ".mkv", ".webm"}:
            method = bot.send_video
            kwargs["video"] = path.open("rb")
        elif kind == "voice" or suffix in {".ogg", ".opus"}:
            method = bot.send_voice
            kwargs["voice"] = path.open("rb")
        elif kind in {"audio", "voice"} or suffix in {".mp3", ".m4a", ".wav"}:
            method = bot.send_audio
            kwargs["audio"] = path.open("rb")
        else:
            method = bot.send_document
            kwargs["document"] = path.open("rb")
        try:
            return await method(**kwargs)
        finally:
            for value in kwargs.values():
                if hasattr(value, "close"):
                    value.close()

    def _remember(self, sent: Any) -> None:
        chat = getattr(sent, "chat", None)
        self.registry.remember(
            getattr(chat, "id", None) or getattr(sent, "chat_id", None),
            getattr(sent, "message_id", None),
        )

    async def _ensure_bot(self) -> Bot:
        async with self._lock:
            if self._bot is None:
                request_kwargs: dict[str, object] = {
                    "connect_timeout": self.settings.tg_admin_bot_connect_timeout,
                    "read_timeout": self.settings.tg_admin_bot_request_timeout,
                    "write_timeout": self.settings.tg_admin_bot_request_timeout,
                    "pool_timeout": self.settings.tg_admin_bot_pool_timeout,
                }
                if self.settings.telegram_bot_proxy:
                    request_kwargs["proxy"] = self.settings.telegram_bot_proxy
                self._bot = Bot(
                    token=self._token or "",
                    request=HTTPXRequest(**request_kwargs),  # type: ignore[arg-type]
                )
            if not self._initialized:
                await self._bot.initialize()
                self._initialized = True
            return self._bot

    async def stop(self) -> None:
        if self._bot is not None and self._initialized:
            await self._bot.shutdown()
            self._initialized = False


class TelegramDelivery:
    """按目标类型把消息分发给用户账号或 Bot。"""

    def __init__(
        self,
        settings: Settings,
        account_manager_getter: Callable[[], TelegramAccountManager | None] | None = None,
        *,
        registry: SentMessageRegistry | None = None,
        user_sender: TelegramUserSender | None = None,
        bot_sender: TelegramBotSender | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry or SentMessageRegistry()
        self.user = user_sender or TelegramUserSender(
            settings,
            account_manager_getter or (lambda: None),
            self.registry,
        )
        self.bot = bot_sender or TelegramBotSender(settings, self.registry)

    @property
    def enabled(self) -> bool:
        return self.user.enabled or self.bot.configured

    @property
    def identities(self) -> list[str]:
        values = [TELEGRAM_TARGET_CHAT]
        if self.bot.configured:
            values.append(TELEGRAM_TARGET_BOT)
        return values

    async def send(
        self,
        target: ForwardTarget,
        message: TelegramForwardMessage,
        text: str,
    ) -> None:
        target_type = (target.target_type or TELEGRAM_TARGET_CHAT).strip()
        if target_type == TELEGRAM_TARGET_BOT:
            await self.bot.send(target, message, text)
            return
        await self.user.send(target, message, text)

    async def stop(self) -> None:
        await self.bot.stop()
