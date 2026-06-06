from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import botpy
from botpy import Client

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class QQTargetInfo:
    target_type: str
    target_id: str
    last_message_id: str | None = None
    display_name: str | None = None
    updated_at: datetime = field(default_factory=_utc_now)


class ForwarderQQClient(Client):
    """QQ WebSocket client.

    The project does not implement QQ-side business commands. Incoming QQ events are
    only used to cache session scene and latest message ids, which QQ Official APIs
    may require for replies in group/channel contexts.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._session_last_message_id: dict[str, str] = {}
        self._session_scene: dict[str, str] = {}
        self._targets: dict[tuple[str, str], QQTargetInfo] = {}

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

    async def on_direct_message_create(self, message: botpy.message.DirectMessage) -> None:
        display_name = getattr(message, "guild_id", None)
        self.remember_session(
            getattr(message, "guild_id", None),
            "dms",
            getattr(message, "id", None),
            display_name=f"频道私信 {display_name}" if display_name else "QQ频道私信",
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
