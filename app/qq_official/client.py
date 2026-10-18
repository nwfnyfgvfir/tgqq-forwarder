from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import botpy
from botpy import Client
from botpy.connection import ConnectionState

from app.platforms import Platform
from app.rules.models import TelegramForwardMessage, TelegramLink

logger = logging.getLogger(__name__)

# QQ 正文里的 @ 占位符，例如 `<@!123456>`；转发前去掉，避免出现在目标平台。
_QQ_MENTION_RE = re.compile(r"<@!?\d+>")
_RECENT_MESSAGE_LIMIT = 500
_RECENT_MESSAGE_TTL_SECONDS = 300.0

MessageCallback = Callable[[TelegramForwardMessage], Awaitable[None]]


def ensure_group_message_create_parser() -> None:
    """Register qq-botpy parser for plain group messages (not only @ mentions)."""

    if getattr(ConnectionState, "_tgqq_group_message_create_parser", False):
        return

    def build_parser(event_name: str) -> Any:
        def parse_message(self, payload: dict[str, Any]) -> None:
            qq_message = botpy.message.GroupMessage(
                self.api,
                payload.get("id", None),
                payload.get("d", {}),
            )
            self._dispatch(event_name, qq_message)

        return parse_message

    ConnectionState.parse_group_message_create = build_parser("group_message_create")
    ConnectionState._tgqq_group_message_create_parser = True


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class QQTargetInfo:
    target_type: str
    target_id: str
    last_message_id: str | None = None
    display_name: str | None = None
    updated_at: datetime = field(default_factory=_utc_now)


def _short_id(value: object, length: int = 8) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    return text if len(text) <= length else text[-length:]


def _clean_text(value: object) -> str:
    text = str(value or "")
    text = _QQ_MENTION_RE.sub("", text)
    return text.strip()


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _attachment_lines(attachments: list[Any]) -> tuple[list[str], list[str]]:
    """把 QQ 附件转成文本占位行与媒体类型（不下载文件）。"""
    lines: list[str] = []
    media_types: list[str] = []
    for item in attachments or []:
        content_type = str(getattr(item, "content_type", "") or "").lower()
        url = str(getattr(item, "url", "") or "").strip()
        filename = str(getattr(item, "filename", "") or "").strip()
        if content_type.startswith("image"):
            label, media_type = "[图片]", "photo"
        elif content_type.startswith("video"):
            label, media_type = "[视频]", "video"
        elif content_type.startswith("audio"):
            label, media_type = "[音频]", "audio"
        else:
            label, media_type = (f"[文件] {filename}" if filename else "[文件]"), "document"
        media_types.append(media_type)
        lines.append(f"{label} {url}".strip())
    return lines, media_types


class ForwarderQQClient(Client):
    """QQ WebSocket client.

    Incoming QQ events serve two purposes:

    * cache the session scene and latest message ids that QQ Official APIs need; and
    * turn user messages into :class:`TelegramForwardMessage` so rules can forward
      QQ → Telegram / 微信.
    """

    def __init__(
        self,
        *args: Any,
        on_message: MessageCallback | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.on_message = on_message
        self._session_last_message_id: dict[str, str] = {}
        self._session_scene: dict[str, str] = {}
        self._targets: dict[tuple[str, str], QQTargetInfo] = {}
        self._recent_message_ids: OrderedDict[str, float] = OrderedDict()

    def remember_session(
        self,
        session_id: str | None,
        scene: str,
        message_id: str | None,
        *,
        display_name: str | None = None,
    ) -> None:
        if not session_id:
            return
        self._session_scene[session_id] = scene
        if message_id:
            self._session_last_message_id[session_id] = message_id
        self._targets[(scene, session_id)] = QQTargetInfo(
            target_type=scene,
            target_id=session_id,
            last_message_id=message_id,
            display_name=display_name,
        )
        logger.info(
            "Cached QQ target scene=%s target_id=%s msg_id=%s display=%s",
            scene,
            session_id,
            message_id,
            display_name,
        )

    def get_last_message_id(self, session_id: str) -> str | None:
        return self._session_last_message_id.get(session_id)

    def get_scene(self, session_id: str) -> str | None:
        return self._session_scene.get(session_id)

    def list_cached_targets(self) -> list[QQTargetInfo]:
        return sorted(
            self._targets.values(),
            key=lambda item: (item.target_type, item.updated_at),
            reverse=True,
        )

    async def on_group_at_message_create(self, message: botpy.message.GroupMessage) -> None:
        self._remember_group_session(message)
        await self._emit_group_message(message)

    async def on_group_message_create(self, message: botpy.message.GroupMessage) -> None:
        self._remember_group_session(message)
        await self._emit_group_message(message)

    def _remember_group_session(self, message: botpy.message.GroupMessage) -> None:
        self.remember_session(
            getattr(message, "group_openid", None),
            "group",
            getattr(message, "id", None),
            display_name="QQ群",
        )

    async def on_at_message_create(self, message: botpy.message.Message) -> None:
        display_name = getattr(message, "channel_id", None)
        self.remember_session(
            getattr(message, "channel_id", None),
            "channel",
            getattr(message, "id", None),
            display_name=f"频道 {display_name}" if display_name else "QQ频道",
        )
        await self._emit(
            message,
            chat_id=getattr(message, "channel_id", None),
            chat_title=f"QQ频道 {_short_id(getattr(message, 'channel_id', None))}",
            chat_type="channel",
        )

    async def on_direct_message_create(self, message: botpy.message.DirectMessage) -> None:
        display_name = getattr(message, "guild_id", None)
        self.remember_session(
            getattr(message, "guild_id", None),
            "dms",
            getattr(message, "id", None),
            display_name=f"频道私信 {display_name}" if display_name else "QQ频道私信",
        )
        await self._emit(
            message,
            chat_id=getattr(message, "guild_id", None),
            chat_title=f"QQ频道私信 {_short_id(getattr(message, 'guild_id', None))}",
            chat_type="private",
        )

    async def on_c2c_message_create(self, message: botpy.message.C2CMessage) -> None:
        author = getattr(message, "author", None)
        openid = getattr(author, "user_openid", None) or getattr(message, "user_openid", None)
        display_name = getattr(author, "username", None) or getattr(author, "id", None)
        self.remember_session(
            openid,
            "c2c",
            getattr(message, "id", None),
            display_name=f"用户 {display_name}" if display_name else "QQ用户",
        )
        await self._emit(
            message,
            chat_id=openid,
            chat_title=f"QQ用户 {_short_id(openid)}",
            chat_type="private",
        )

    # ---------------------------------------------------------------- 入站转发

    async def _emit_group_message(self, message: botpy.message.GroupMessage) -> None:
        await self._emit(
            message,
            chat_id=getattr(message, "group_openid", None),
            chat_title=f"QQ群 {_short_id(getattr(message, 'group_openid', None))}",
            chat_type="group",
        )

    async def _emit(
        self,
        message: Any,
        *,
        chat_id: str | None,
        chat_title: str,
        chat_type: str,
    ) -> None:
        if self.on_message is None or not chat_id:
            return
        message_id = str(getattr(message, "id", "") or "").strip()
        if not message_id or self._is_duplicate(message_id):
            return

        author = getattr(message, "author", None)
        sender_id = (
            getattr(author, "member_openid", None)
            or getattr(author, "user_openid", None)
            or getattr(author, "id", None)
        )
        sender_name = (
            getattr(author, "username", None)
            or getattr(author, "nick", None)
            or sender_id
        )
        text = _clean_text(getattr(message, "content", None))
        attachment_lines, media_types = _attachment_lines(
            list(getattr(message, "attachments", None) or [])
        )
        if attachment_lines:
            text = "\n".join([text, *attachment_lines]).strip()
        if not text and not media_types:
            logger.debug("Skip empty QQ message id=%s chat=%s", message_id, chat_id)
            return

        forward = TelegramForwardMessage(
            message_id=message_id,
            chat_id=chat_id,
            chat_title=chat_title,
            chat_type=chat_type,
            sender_id=sender_id,
            sender_username=getattr(author, "username", None),
            sender_display_name=str(sender_name) if sender_name else None,
            sender_is_bot=bool(getattr(author, "bot", False)),
            text=text,
            media_type=media_types[0] if media_types else None,
            media_path=None,
            date=_parse_timestamp(getattr(message, "timestamp", None)),
            links=[
                TelegramLink(text=url, url=url, source="visible_url")
                for url in _extract_urls(text)
            ],
            source_platform=Platform.QQ.value,
        )
        try:
            await self.on_message(forward)
        except Exception:
            logger.exception("QQ on_message callback failed id=%s chat=%s", message_id, chat_id)

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.monotonic()
        while self._recent_message_ids:
            oldest_id, seen_at = next(iter(self._recent_message_ids.items()))
            if (
                now - seen_at <= _RECENT_MESSAGE_TTL_SECONDS
                and len(self._recent_message_ids) < _RECENT_MESSAGE_LIMIT
            ):
                break
            self._recent_message_ids.pop(oldest_id, None)
        if message_id in self._recent_message_ids:
            return True
        self._recent_message_ids[message_id] = now
        return False


_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,!?;:，。！？；：、)]}）】》")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls
