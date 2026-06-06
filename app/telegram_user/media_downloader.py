from __future__ import annotations

import logging
from pathlib import Path

from telethon import events

logger = logging.getLogger(__name__)


class TelegramMediaDownloader:
    def __init__(self, media_dir: Path, *, enabled: bool = True, max_media_mb: int = 20) -> None:
        self.media_dir = media_dir
        self.enabled = enabled
        self.max_media_bytes = max_media_mb * 1024 * 1024
        self.media_dir.mkdir(parents=True, exist_ok=True)

    async def download(self, event: events.NewMessage.Event) -> Path | None:
        if not self.enabled or not event.message or not event.message.media:
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
