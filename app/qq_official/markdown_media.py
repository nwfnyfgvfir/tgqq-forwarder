"""Upload local images as QQ Markdown materials without a public image host."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from botpy.http import Route
from PIL import Image

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_IMAGE_FORMATS = {
    "PNG": (".png", "image/png"),
    "JPEG": (".jpg", "image/jpeg"),
    "GIF": (".gif", "image/gif"),
    "WEBP": (".webp", "image/webp"),
}


class MarkdownMediaUnavailable(Exception):
    """Use separate media and Markdown messages when combined sending is unavailable."""


class QQMarkdownRoute(Route):
    DOMAIN = "api.bot.qq.com"


@dataclass(frozen=True, slots=True)
class MarkdownImage:
    image_id: str
    reference: str
    width: int
    height: int

    def markdown(self) -> str:
        return f"![图片 #{self.width}px #{self.height}px]({self.reference})"


def _read_image(path: Path) -> tuple[bytes, str, str, int, int]:
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise MarkdownMediaUnavailable("image exceeds QQ material size limit")
    with path.open("rb") as source:
        data = source.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise MarkdownMediaUnavailable("image exceeds QQ material size limit")
    with Image.open(BytesIO(data)) as image:
        if image.format not in _IMAGE_FORMATS:
            raise MarkdownMediaUnavailable("unsupported QQ Markdown image format")
        suffix, content_type = _IMAGE_FORMATS[image.format]
        width, height = image.size
        image.verify()
    return data, path.stem[:100] + suffix, content_type, width, height


class MarkdownImageUploader:
    def __init__(
        self,
        request: Callable[..., Awaitable[Any]],
        *,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._request = request
        self._timeout = timeout
        self._client = client

    async def upload(self, path: Path) -> MarkdownImage:
        data, file_name, content_type, width, height = await asyncio.to_thread(_read_image, path)
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        prepared = await self._request(
            QQMarkdownRoute("POST", "/v2/media-images/prepare"),
            json={
                "file_name": file_name,
                "file_size": len(data),
                "content_type": content_type,
                "md5": md5,
                "sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest(),
            },
        )
        if not isinstance(prepared, dict):
            raise MarkdownMediaUnavailable("invalid QQ image upload response")
        upload_id = prepared.get("upload_id")
        presigned_url = prepared.get("presigned_url")
        parsed_url = urlsplit(presigned_url) if isinstance(presigned_url, str) else None
        if (
            not isinstance(upload_id, str)
            or not upload_id
            or parsed_url is None
            or parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise MarkdownMediaUnavailable("invalid QQ image upload destination")
        if self._client is None:
            # A separate client keeps the QQ bot's Authorization header off the signed PUT.
            self._client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
        response = await self._client.put(
            presigned_url,
            content=data,
            headers={"Content-Type": content_type},
            follow_redirects=False,
        )
        response.raise_for_status()
        etag = response.headers.get("ETag", "").strip('"').lower()
        if etag != md5:
            raise MarkdownMediaUnavailable("QQ image upload checksum mismatch")
        completed = await self._request(
            QQMarkdownRoute("POST", "/v2/media-images/complete"),
            json={"upload_id": upload_id, "md5": etag},
        )
        if not isinstance(completed, dict):
            raise MarkdownMediaUnavailable("invalid QQ image completion response")
        image_id, reference = completed.get("image_id"), completed.get("url")
        if not isinstance(image_id, str) or not image_id:
            raise MarkdownMediaUnavailable("missing QQ material ID")
        if not isinstance(reference, str) or not re.fullmatch(
            r"qqbot-media://image/v1/[A-Za-z0-9._~%+=/-]+", reference
        ):
            await self.release(image_id)
            raise MarkdownMediaUnavailable("invalid QQ Markdown image reference")
        return MarkdownImage(image_id, reference, width, height)

    async def release(self, image_id: str) -> None:
        try:
            await self._request(
                QQMarkdownRoute("DELETE", f"/v2/media-images/{quote(image_id, safe='')}/delete")
            )
        except Exception as error:
            # QQ copies images on send, so releasing our material preserves sent messages.
            logger.warning("QQ Markdown material cleanup failed: %s", type(error).__name__)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
