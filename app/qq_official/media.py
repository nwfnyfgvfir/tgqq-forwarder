"""QQ 入站附件落盘。

QQ 官方机器人推送的 ``attachments`` 只有 ``url`` / ``content_type`` / ``filename``，
URL 是带时效的 CDN 链接，必须收到消息后立刻下载，否则转发时就 404 了。

下载成功的附件会作为真实文件参与后续投递（Telegram 用 ``send_file``，微信走
``getuploadurl`` 上传），下载失败则退化成 ``[图片] https://...`` 这样的文本说明，
保证消息本身不会丢。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MAGIC_SUFFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
)
CONTENT_TYPE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/amr": ".amr",
    "audio/silk": ".silk",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
}
VOICE_SUFFIXES = {".ogg", ".opus", ".amr", ".silk"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".aac", ".flac"}
# 单条消息最多下载这么多附件，避免异常消息把队列拖住。
MAX_ATTACHMENTS = 9


def sanitize_path_segment(value: object) -> str:
    text = _SAFE_NAME_RE.sub("_", str(value or "")).strip("_")
    if not text:
        return "unknown"
    if len(text) > 48:
        digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:8]
        text = f"{text[:40]}_{digest}"
    return text


def guess_suffix_from_bytes(content: bytes) -> str | None:
    for magic, suffix in _MAGIC_SUFFIXES:
        if content.startswith(magic):
            return suffix
    return None


def media_type_for(content_type: str | None, suffix: str) -> str:
    ctype = (content_type or "").lower()
    if ctype.startswith("image/"):
        return "photo"
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("audio/"):
        return "voice" if suffix in VOICE_SUFFIXES else "audio"
    if suffix in IMAGE_SUFFIXES:
        return "photo"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in VOICE_SUFFIXES:
        return "voice"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return "document"


def _safe_suffix(value: str) -> str:
    text = (value or "").strip().lower()
    if not text.startswith(".") or len(text) > 12:
        return ""
    if any(char in text for char in "/\\?#&=% "):
        return ""
    return text


class QQAttachmentDownloader:
    """把 QQ 附件下载到 ``media/qq/<chat_id>/``。"""

    def __init__(
        self,
        media_dir: Path,
        *,
        enabled: bool = True,
        max_media_mb: int = 20,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.root = Path(media_dir) / "qq"
        self.enabled = enabled
        self.max_bytes = max(0, int(max_media_mb)) * 1024 * 1024
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    def directory_for(self, chat_id: object) -> Path:
        return self.root / sanitize_path_segment(chat_id)

    async def download(
        self,
        attachments: list[Any],
        *,
        chat_id: object,
        message_id: object,
    ) -> list[tuple[int, Path, str, str | None]]:
        """返回 ``[(附件下标, 路径, 媒体类型, 原始文件名)]``；失败的附件不在结果里。"""
        if not self.enabled or not attachments:
            return []
        results: list[tuple[int, Path, str, str | None]] = []
        for index, item in enumerate(attachments[:MAX_ATTACHMENTS]):
            saved = await self._download_one(
                item,
                chat_id=chat_id,
                message_id=message_id,
                index=index,
            )
            if saved is not None:
                results.append((index, saved[0], saved[1], saved[2]))
        return results

    async def _download_one(
        self,
        item: Any,
        *,
        chat_id: object,
        message_id: object,
        index: int,
    ) -> tuple[Path, str, str | None] | None:
        url = str(getattr(item, "url", "") or "").strip()
        if not url:
            return None
        if not url.lower().startswith(("http://", "https://")):
            logger.warning("Skip QQ attachment with unsupported url scheme: %s", url[:64])
            return None
        content_type = str(getattr(item, "content_type", "") or "").strip()
        filename = str(getattr(item, "filename", "") or "").strip()
        try:
            content, response_type = await self._fetch(url)
        except (httpx.HTTPError, OSError, ValueError):
            logger.warning("Failed to download QQ attachment url=%s", url[:120], exc_info=True)
            return None
        if content is None:
            return None

        content_type = content_type or response_type
        suffix = self._suffix(filename, content_type, content)
        directory = self.directory_for(chat_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stem = f"{int(time.time() * 1000)}_{sanitize_path_segment(message_id)}_{index}"
            path = directory / f"{stem}{suffix}"
            path.write_bytes(content)
        except OSError:
            logger.exception("Failed to write QQ attachment to %s", directory)
            return None
        logger.info(
            "Downloaded QQ attachment chat=%s message=%s index=%s size=%s type=%s",
            chat_id,
            message_id,
            index,
            len(content),
            content_type or suffix,
        )
        return path, media_type_for(content_type, suffix), filename or None

    async def _fetch(self, url: str) -> tuple[bytes | None, str]:
        client = await self._ensure_client()
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                logger.warning(
                    "QQ attachment responded %s url=%s", response.status_code, url[:120]
                )
                return None, ""
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            declared = response.headers.get("content-length")
            if declared and self.max_bytes:
                try:
                    if int(declared) > self.max_bytes:
                        logger.warning(
                            "Skip QQ attachment larger than limit size=%s limit=%s",
                            declared,
                            self.max_bytes,
                        )
                        return None, content_type
                except ValueError:
                    pass
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if self.max_bytes and len(buffer) > self.max_bytes:
                    logger.warning(
                        "Skip QQ attachment larger than limit size>%s limit=%s",
                        len(buffer),
                        self.max_bytes,
                    )
                    return None, content_type
        return bytes(buffer), content_type

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "tgqq-forwarder/1.0"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    @staticmethod
    def _suffix(filename: str, content_type: str, content: bytes) -> str:
        suffix = _safe_suffix(Path(filename).suffix) if filename else ""
        if suffix:
            return suffix
        suffix = CONTENT_TYPE_SUFFIXES.get((content_type or "").lower(), "")
        if suffix:
            return suffix
        return guess_suffix_from_bytes(content) or ".bin"
