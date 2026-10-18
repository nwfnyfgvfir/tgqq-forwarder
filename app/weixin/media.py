"""微信媒体落盘与上传。

入站：``item_list`` 里的媒体带 ``encrypt_query_param`` + ``aes_key``，
从 CDN 下载后按 AES-ECB 解密再落盘。

出站：先 ``getuploadurl`` 换 CDN 上传地址，加密上传后用响应头
``x-encrypted-param`` 作为 ``encrypt_query_param`` 组进 ``item_list``。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.weixin.client import (
    WeixinIlinkClient,
    build_file_item,
    build_image_item,
    build_video_item,
)
from app.weixin.models import (
    ITEM_TYPE_TO_MEDIA_TYPE,
    MEDIA_TYPE_TO_UPLOAD_TYPE,
    WeixinApiError,
    WeixinItemType,
    WeixinUploadMediaType,
)

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_IMAGE_SUFFIXES = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",
}
_DEFAULT_SUFFIX = {
    WeixinItemType.IMAGE: ".jpg",
    WeixinItemType.VOICE: ".silk",
    WeixinItemType.VIDEO: ".mp4",
    WeixinItemType.FILE: ".bin",
}


def sanitize_path_segment(value: str) -> str:
    text = _SAFE_NAME_RE.sub("_", value or "").strip("_")
    if not text:
        text = "unknown"
    if len(text) > 48:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        text = f"{text[:40]}_{digest}"
    return text


def guess_suffix_from_bytes(content: bytes) -> str | None:
    for magic, suffix in _IMAGE_SUFFIXES.items():
        if content.startswith(magic):
            return suffix
    return None


class WeixinMediaStore:
    def __init__(
        self,
        media_dir: Path,
        account_id: str,
        *,
        enabled: bool = True,
        max_media_mb: int = 20,
    ) -> None:
        self.root = Path(media_dir) / "weixin" / account_id
        self.account_id = account_id
        self.enabled = enabled
        self.max_bytes = max(0, int(max_media_mb)) * 1024 * 1024

    def directory_for(self, user_id: str) -> Path:
        return self.root / sanitize_path_segment(user_id)

    # ---------------------------------------------------------------- 入站

    async def download_inbound(
        self,
        client: WeixinIlinkClient,
        item: dict[str, Any],
        *,
        from_user_id: str,
    ) -> tuple[Path, str, str | None] | None:
        """下载并解密一条入站媒体，返回 ``(路径, 媒体类型, 原始文件名)``。"""
        if not self.enabled:
            return None
        item_type = _item_type(item)
        media_type = ITEM_TYPE_TO_MEDIA_TYPE.get(item_type)
        if media_type is None:
            return None
        params = extract_media_params(item)
        if params is None:
            logger.debug("weixin media item without media params type=%s", int(item_type))
            return None
        encrypted_query_param, aes_key = params
        try:
            content = await client.download_media(
                encrypted_query_param=encrypted_query_param,
                aes_key=aes_key,
            )
        except (WeixinApiError, ValueError):
            logger.warning("Failed to download Weixin media type=%s", int(item_type), exc_info=True)
            return None
        if self.max_bytes and len(content) > self.max_bytes:
            logger.warning(
                "Skip Weixin media larger than limit size=%s limit=%s",
                len(content),
                self.max_bytes,
            )
            return None

        suffix = self._suffix_for(item, item_type, content)
        directory = self.directory_for(from_user_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{int(time.time() * 1000)}_{item_type.name.lower()}{suffix}"
        try:
            path.write_bytes(content)
        except OSError:
            logger.exception("Failed to write Weixin media to %s", path)
            return None
        return path, media_type, self._original_name(item, item_type)

    @staticmethod
    def _original_name(item: dict[str, Any], item_type: WeixinItemType) -> str | None:
        """只有文件类消息带原始文件名，图片/语音/视频由客户端自己命名。"""
        if item_type != WeixinItemType.FILE:
            return None
        file_name = str((item.get("file_item") or {}).get("file_name") or "").strip()
        return file_name or None

    def _suffix_for(self, item: dict[str, Any], item_type: WeixinItemType, content: bytes) -> str:
        if item_type == WeixinItemType.IMAGE:
            return guess_suffix_from_bytes(content) or _DEFAULT_SUFFIX[item_type]
        if item_type == WeixinItemType.FILE:
            file_name = str((item.get("file_item") or {}).get("file_name") or "")
            suffix = Path(file_name).suffix
            return suffix if suffix and len(suffix) <= 12 else _DEFAULT_SUFFIX[item_type]
        return _DEFAULT_SUFFIX[item_type]

    # ---------------------------------------------------------------- 出站

    async def build_outbound_items(
        self,
        client: WeixinIlinkClient,
        *,
        target_user_id: str,
        media_paths: list[Path],
        media_types: list[str | None],
        file_names: list[str | None] | None = None,
        caption: str | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index, media_path in enumerate(media_paths):
            media_type = media_types[index] if index < len(media_types) else None
            upload_type = _upload_type(media_type, media_path)
            try:
                media, padded_size = await client.upload_and_build_media_payload(
                    to_user_id=target_user_id,
                    media_path=media_path,
                    upload_media_type=int(upload_type),
                )
            except (WeixinApiError, OSError):
                logger.warning(
                    "Failed to upload media to Weixin path=%s", media_path, exc_info=True
                )
                continue

            if upload_type == WeixinUploadMediaType.IMAGE:
                items.append(build_image_item(media, size=padded_size))
            elif upload_type == WeixinUploadMediaType.VIDEO:
                items.append(build_video_item(media, size=padded_size))
            else:
                # 文件名必须带扩展名，否则微信端无法识别格式；caption 只用于兜底命名。
                requested = file_names[index] if file_names and index < len(file_names) else None
                items.append(
                    build_file_item(
                        media,
                        file_name=_display_file_name(requested, media_path, caption),
                        raw_size=media_path.stat().st_size,
                    )
                )
        return items


def _item_type(item: dict[str, Any]) -> WeixinItemType:
    try:
        return WeixinItemType(int(item.get("type") or 0))
    except ValueError:
        return WeixinItemType.TEXT


def extract_media_params(item: dict[str, Any]) -> tuple[str, str] | None:
    """从 item 中取出 ``(encrypt_query_param, aes_key)``。"""
    item_type = _item_type(item)
    container_key = {
        WeixinItemType.IMAGE: "image_item",
        WeixinItemType.VOICE: "voice_item",
        WeixinItemType.FILE: "file_item",
        WeixinItemType.VIDEO: "video_item",
    }.get(item_type)
    if container_key is None:
        return None
    container = item.get(container_key)
    if not isinstance(container, dict):
        return None
    media = container.get("media")
    params: dict[str, Any] = media if isinstance(media, dict) else {}

    encrypted_query_param = str(
        params.get("encrypt_query_param")
        or container.get("encrypt_query_param")
        or params.get("encrypted_query_param")
        or ""
    ).strip()
    aes_key = str(
        params.get("aes_key")
        or container.get("aeskey")
        or container.get("aes_key")
        or ""
    ).strip()
    if not encrypted_query_param or not aes_key:
        return None
    return encrypted_query_param, aes_key


def _display_file_name(
    requested: str | None,
    media_path: Path,
    fallback: str | None = None,
) -> str:
    """决定 file_item.file_name：优先原始文件名，且必须带扩展名。"""
    name = (requested or "").strip().replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        name = media_path.name if media_path.suffix else (fallback or media_path.name)
    if not Path(name).suffix and media_path.suffix:
        name = f"{name}{media_path.suffix}"
    return name


def _upload_type(media_type: str | None, media_path: Path) -> WeixinUploadMediaType:
    if media_type and media_type in MEDIA_TYPE_TO_UPLOAD_TYPE:
        return MEDIA_TYPE_TO_UPLOAD_TYPE[media_type]
    suffix = media_path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return WeixinUploadMediaType.IMAGE
    if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
        return WeixinUploadMediaType.VIDEO
    return WeixinUploadMediaType.FILE
