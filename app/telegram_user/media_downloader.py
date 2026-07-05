from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telethon import events
from telethon.tl.types import MessageMediaWebPage

logger = logging.getLogger(__name__)


def is_link_preview_media(message: Any) -> bool:
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaWebPage):
        return True
    if not getattr(message, "web_preview", None):
        return False
    return not any(
        getattr(message, attr, None)
        for attr in ("photo", "video", "voice", "audio", "document")
    )


class TelegramMediaDownloader:
    def __init__(
        self,
        media_dir: Path,
        *,
        enabled: bool = True,
        max_media_mb: int = 20,
        download_link_preview_media: bool = False,
    ) -> None:
        self.media_dir = media_dir
        self.enabled = enabled
        self.max_media_bytes = max_media_mb * 1024 * 1024
        self.download_link_preview_media = download_link_preview_media
        self.media_dir.mkdir(parents=True, exist_ok=True)

    async def download(self, event: events.NewMessage.Event) -> Path | None:
        if not self.enabled or not event.message or not event.message.media:
            return None
        if is_link_preview_media(event.message) and not self.download_link_preview_media:
            return None

        size = getattr(getattr(event.message, "file", None), "size", None)
        if size and size > self.max_media_bytes:
            logger.warning("Skip media larger than configured limit: %s bytes", size)
            return None

        chat_part = str(event.chat_id or "unknown")
        target_dir = self.media_dir / chat_part
        target_dir.mkdir(parents=True, exist_ok=True)
        downloaded = await event.message.download_media(file=str(target_dir))
        return Path(downloaded) if downloaded else None
