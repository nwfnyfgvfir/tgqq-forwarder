from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.storage.models import QQTargetType


@dataclass(slots=True)
class QQOutboundMessage:
    target_type: QQTargetType | str
    target_id: str
    text: str
    media_path: Path | None = None
    media_type: str | None = None
    guild_id: str | None = None
    channel_id: str | None = None
