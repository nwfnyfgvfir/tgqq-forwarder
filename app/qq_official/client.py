from __future__ import annotations

import logging
from typing import Any

import botpy
from botpy import Client

logger = logging.getLogger(__name__)


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

    def remember_session(self, session_id: str | None, scene: str, message_id: str | None) -> None:
        if not session_id:
            return
        self._session_scene[session_id] = scene
        if message_id:
            self._session_last_message_id[session_id] = message_id
        logger.info("Cached QQ session scene=%s session_id=%s msg_id=%s", scene, session_id, message_id)

    def get_last_message_id(self, session_id: str) -> str | None:
        return self._session_last_message_id.get(session_id)

    def get_scene(self, session_id: str) -> str | None:
        return self._session_scene.get(session_id)

    async def on_group_at_message_create(self, message: botpy.message.GroupMessage) -> None:
        self.remember_session(
            getattr(message, "group_openid", None),
            "group",
            getattr(message, "id", None),
        )

    async def on_at_message_create(self, message: botpy.message.Message) -> None:
        self.remember_session(
            getattr(message, "channel_id", None),
            "channel",
            getattr(message, "id", None),
        )

    async def on_direct_message_create(self, message: botpy.message.DirectMessage) -> None:
        self.remember_session(
            getattr(message, "guild_id", None),
            "dms",
            getattr(message, "id", None),
        )

    async def on_c2c_message_create(self, message: botpy.message.C2CMessage) -> None:
        author = getattr(message, "author", None)
        openid = getattr(author, "user_openid", None) or getattr(message, "user_openid", None)
        self.remember_session(openid, "c2c", getattr(message, "id", None))
