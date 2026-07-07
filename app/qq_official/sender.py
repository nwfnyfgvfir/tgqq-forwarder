from __future__ import annotations

import asyncio
import base64
import logging
import random
from pathlib import Path
from typing import Any

import aiofiles
import botpy
import botpy.errors
from botpy.http import Route
from botpy.types import message as qq_message
from botpy.types.message import MarkdownPayload, Media
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings
from app.qq_official.client import ForwarderQQClient, QQTargetInfo
from app.qq_official.models import QQOutboundMessage
from app.storage.models import QQTargetType

logger = logging.getLogger(__name__)

IMAGE_FILE_TYPE = 1
VIDEO_FILE_TYPE = 2
VOICE_FILE_TYPE = 3
FILE_FILE_TYPE = 4


def _patch_qq_botpy_formdata() -> None:
    try:
        from botpy.http import _FormData  # type: ignore

        if not hasattr(_FormData, "_is_processed"):
            _FormData._is_processed = False
    except Exception:
        logger.debug("Skip qq-botpy FormData compatibility patch", exc_info=True)


_patch_qq_botpy_formdata()


_qq_retry = retry(
    retry=retry_if_exception_type(
        (
            botpy.errors.ServerError,
            botpy.errors.SequenceNumberError,
            OSError,
            asyncio.TimeoutError,
        )
    ),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)


class QQOfficialSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        intents = botpy.Intents(
            public_messages=settings.qq_enable_group_c2c,
            public_guild_messages=True,
            direct_message=settings.qq_enable_guild_direct_message,
        )
        self.client = ForwarderQQClient(intents=intents, bot_log=False, timeout=20)
        self._task: asyncio.Task | None = None
        self._started = asyncio.Event()

    def list_cached_targets(self) -> list[QQTargetInfo]:
        return self.client.list_cached_targets()

    @property
    def status(self) -> str:
        if self._task is None:
            return "not-started"
        if self._task.cancelled():
            return "stopped"
        if self._task.done():
            if exc := self._task.exception():
                return f"failed: {exc}"
            return "stopped"
        return "running"

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="qq-official-websocket")
        await asyncio.sleep(0)

    async def _run(self) -> None:
        logger.info("Starting QQ Official Bot WebSocket client")
        self._started.set()
        await self.client.start(
            appid=self.settings.qq_bot_appid,
            secret=self.settings.qq_bot_secret,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        try:
            await self.client.close()
        except Exception:
            logger.exception("Failed to close QQ client cleanly")
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("QQ Official Bot WebSocket client stopped")

    async def send(self, outbound: QQOutboundMessage) -> Any:
        target_type = QQTargetType(str(outbound.target_type))
        text = self._truncate_text(outbound.text)
        media_paths = outbound.media_paths or ([outbound.media_path] if outbound.media_path else [])
        if media_paths:
            return await self._send_media_sequence(outbound, target_type, text, media_paths)
        return await self._send_text(outbound, target_type, text)

    async def _send_media_sequence(
        self,
        outbound: QQOutboundMessage,
        target_type: QQTargetType,
        text: str,
        media_paths: list[Path],
    ) -> list[Any]:
        results: list[Any] = []
        total = len(media_paths)
        for index, media_path in enumerate(media_paths, start=1):
            media_type = None
            if index <= len(outbound.media_types):
                media_type = outbound.media_types[index - 1]
            item_text = text if index == 1 else f"[继续发送媒体 {index}/{total}]"
            item = QQOutboundMessage(
                target_type=outbound.target_type,
                target_id=outbound.target_id,
                text=item_text,
                media_path=media_path,
                media_type=media_type,
                guild_id=outbound.guild_id,
                channel_id=outbound.channel_id,
            )
            results.append(await self._send_media(item, target_type, item_text))
        return results

    async def _send_text(
        self,
        outbound: QQOutboundMessage,
        target_type: QQTargetType,
        text: str,
    ) -> Any:
        if target_type == QQTargetType.GROUP:
            payload = self._v2_payload(outbound.target_id, text, include_msg_id=True)
            return await self._post_group_message(outbound.target_id, payload)

        if target_type == QQTargetType.C2C:
            payload = self._v2_payload(outbound.target_id, text, include_msg_id=False)
            return await self._post_c2c_message(outbound.target_id, **payload)

        if target_type == QQTargetType.CHANNEL:
            payload = self._guild_payload(outbound.target_id, text)
            return await self._post_channel_message(outbound.target_id, payload)

        if target_type == QQTargetType.DMS:
            guild_id = outbound.guild_id or outbound.target_id
            payload = self._guild_payload(guild_id, text)
            return await self._post_dms_message(guild_id, payload)

        raise ValueError(f"Unsupported QQ target type: {target_type}")

    async def _send_media(
        self,
        outbound: QQOutboundMessage,
        target_type: QQTargetType,
        text: str,
    ) -> Any:
        media_path = outbound.media_path
        if media_path is None:
            return await self._send_text(outbound, target_type, text)
        media_type = self._qq_media_type(outbound.media_type, media_path)

        if target_type == QQTargetType.GROUP:
            payload = self._v2_payload(outbound.target_id, text, include_msg_id=True)
            media = await self._upload_group_or_c2c_media(
                media_path,
                media_type,
                group_openid=outbound.target_id,
            )
            payload.update({"media": media, "msg_type": 7, "content": text})
            payload.pop("markdown", None)
            return await self._post_group_message(outbound.target_id, payload)

        if target_type == QQTargetType.C2C:
            payload = self._v2_payload(outbound.target_id, text, include_msg_id=False)
            media = await self._upload_group_or_c2c_media(
                media_path,
                media_type,
                openid=outbound.target_id,
            )
            payload.update({"media": media, "msg_type": 7, "content": text})
            payload.pop("markdown", None)
            return await self._post_c2c_message(outbound.target_id, **payload)

        if target_type == QQTargetType.CHANNEL:
            payload = self._guild_payload(outbound.target_id, text)
            if media_type == IMAGE_FILE_TYPE:
                payload["file_image"] = str(media_path)
            else:
                payload["content"] = f"{text}\n[媒体文件]".strip()
            return await self._post_channel_message(outbound.target_id, payload)

        if target_type == QQTargetType.DMS:
            guild_id = outbound.guild_id or outbound.target_id
            payload = self._guild_payload(guild_id, text)
            if media_type == IMAGE_FILE_TYPE:
                payload["file_image"] = str(media_path)
            else:
                payload["content"] = f"{text}\n[媒体文件]".strip()
            return await self._post_dms_message(guild_id, payload)

        raise ValueError(f"Unsupported QQ target type: {target_type}")

    def _v2_payload(self, session_id: str, text: str, *, include_msg_id: bool) -> dict[str, Any]:
        payload: dict[str, Any]
        if self.settings.qq_use_markdown:
            payload = {"markdown": MarkdownPayload(content=text), "msg_type": 2}
        else:
            payload = {"content": text, "msg_type": 0}
        payload["msg_seq"] = random.randint(1, 10000)

        if include_msg_id:
            msg_id = self.client.get_last_message_id(session_id)
            if msg_id:
                payload["msg_id"] = msg_id
            elif not self.settings.qq_allow_send_without_cached_msg_id:
                raise RuntimeError(
                    "No cached QQ msg_id for this target. Send one message to the bot first "
                    "or set QQ_ALLOW_SEND_WITHOUT_CACHED_MSG_ID=true."
                )
        return payload

    def _guild_payload(self, session_id: str, text: str) -> dict[str, Any]:
        payload: dict[str, Any]
        if self.settings.qq_use_markdown:
            payload = {"markdown": MarkdownPayload(content=text)}
        else:
            payload = {"content": text}
        msg_id = self.client.get_last_message_id(session_id)
        if msg_id:
            payload["msg_id"] = msg_id
        elif not self.settings.qq_allow_send_without_cached_msg_id:
            raise RuntimeError(
                "No cached QQ msg_id for this target. Send one message to the bot first "
                "or set QQ_ALLOW_SEND_WITHOUT_CACHED_MSG_ID=true."
            )
        return payload

    @_qq_retry
    async def _post_group_message(self, group_openid: str, payload: dict[str, Any]) -> Any:
        try:
            return await self.client.api.post_group_message(group_openid=group_openid, **payload)
        except botpy.errors.ServerError as err:
            if not self._is_markdown_not_allowed(err, payload):
                raise
            return await self.client.api.post_group_message(
                group_openid=group_openid,
                **self._plain_text_fallback_payload(payload),
            )

    @_qq_retry
    async def _post_channel_message(self, channel_id: str, payload: dict[str, Any]) -> Any:
        try:
            return await self.client.api.post_message(channel_id=channel_id, **payload)
        except botpy.errors.ServerError as err:
            if not self._is_markdown_not_allowed(err, payload):
                raise
            return await self.client.api.post_message(
                channel_id=channel_id,
                **self._plain_text_fallback_payload(payload),
            )

    @_qq_retry
    async def _post_dms_message(self, guild_id: str, payload: dict[str, Any]) -> Any:
        try:
            return await self.client.api.post_dms(guild_id=guild_id, **payload)
        except botpy.errors.ServerError as err:
            if not self._is_markdown_not_allowed(err, payload):
                raise
            return await self.client.api.post_dms(
                guild_id=guild_id,
                **self._plain_text_fallback_payload(payload),
            )

    @staticmethod
    def _is_markdown_not_allowed(err: Exception, payload: dict[str, Any]) -> bool:
        return bool(payload.get("markdown") and "不允许发送原生 markdown" in str(err))

    @staticmethod
    def _plain_text_fallback_payload(payload: dict[str, Any]) -> dict[str, Any]:
        fallback = payload.copy()
        markdown = fallback.pop("markdown", None)
        fallback["content"] = getattr(markdown, "content", None) or fallback.get("content", "")
        if "msg_type" in fallback:
            fallback["msg_type"] = 0
        return fallback

    @_qq_retry
    async def _post_c2c_message(
        self,
        openid: str,
        msg_type: int = 0,
        content: str | None = None,
        media: qq_message.Media | None = None,
        msg_id: str | None = None,
        msg_seq: int | None = 1,
        markdown: qq_message.MarkdownPayload | None = None,
        **kwargs: Any,
    ) -> qq_message.Message | dict[str, Any] | None:
        payload = {
            "msg_type": msg_type,
            "content": content,
            "media": media,
            "msg_id": msg_id,
            "msg_seq": msg_seq,
            "markdown": markdown,
            **kwargs,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        route = Route("POST", "/v2/users/{openid}/messages", openid=openid)
        try:
            result = await self.client.api._http.request(route, json=payload)
        except botpy.errors.ServerError as err:
            if not self._is_markdown_not_allowed(err, payload):
                raise
            result = await self.client.api._http.request(
                route,
                json=self._plain_text_fallback_payload(payload),
            )
        if isinstance(result, dict):
            return qq_message.Message(**result)
        return result

    @_qq_retry
    async def _upload_group_or_c2c_media(
        self,
        media_path: Path,
        file_type: int,
        *,
        openid: str | None = None,
        group_openid: str | None = None,
    ) -> Media:
        async with aiofiles.open(media_path, "rb") as file:
            file_data = base64.b64encode(await file.read()).decode("utf-8")

        payload: dict[str, Any] = {
            "file_type": file_type,
            "file_data": file_data,
            "srv_send_msg": False,
        }
        if openid:
            payload["openid"] = openid
            route = Route("POST", "/v2/users/{openid}/files", openid=openid)
        elif group_openid:
            payload["group_openid"] = group_openid
            route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
        else:
            raise ValueError("Either openid or group_openid is required")

        result = await self.client.api._http.request(route, json=payload)
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected QQ media upload response: {result!r}")
        return Media(
            file_uuid=result["file_uuid"],
            file_info=result["file_info"],
            ttl=result.get("ttl", 0),
        )

    @staticmethod
    def _qq_media_type(media_type: str | None, media_path: Path) -> int:
        suffix = media_path.suffix.lower()
        if media_type == "photo" or suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            return IMAGE_FILE_TYPE
        if media_type == "video" or suffix in {".mp4", ".mov", ".mkv", ".webm"}:
            return VIDEO_FILE_TYPE
        if media_type in {"voice", "audio"} or suffix in {".mp3", ".wav", ".ogg", ".m4a"}:
            return VOICE_FILE_TYPE
        return FILE_FILE_TYPE

    @staticmethod
    def _truncate_text(text: str) -> str:
        text = text.strip()
        if not text:
            return "[empty message]"
        if len(text) <= 3900:
            return text
        return text[:3890] + "\n...[truncated]"
