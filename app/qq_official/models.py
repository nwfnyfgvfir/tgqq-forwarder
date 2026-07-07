from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.storage.models import QQTargetType


@dataclass(slots=True)
class QQOutboundMessage:
    target_type: QQTargetType | str
    target_id: str
    text: str
    media_path: Path | None = None
    media_type: str | None = None
    media_paths: list[Path] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    media_caption: str | None = None
    guild_id: str | None = None
    channel_id: str | None = None

    def __post_init__(self) -> None:
        if self.media_path and not self.media_paths:
            self.media_paths.append(self.media_path)
        if self.media_type and not self.media_types:
            self.media_types.append(self.media_type)
