from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class TelegramForwardMessage:
    message_id: int
    chat_id: int | None
    chat_title: str | None
    chat_type: str
    sender_id: int | None
    sender_username: str | None
    sender_display_name: str | None
    sender_is_bot: bool
    text: str
    media_type: str | None
    media_path: Path | None
    date: datetime | None
    raw_url: str | None = None

    @property
    def sender_name(self) -> str:
        return self.sender_display_name or self.sender_username or str(self.sender_id or "unknown")

    @property
    def media_note(self) -> str:
        if not self.media_type:
            return ""
        location = f" {self.media_path}" if self.media_path else ""
        return f"[media: {self.media_type}{location}]"
