from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class TelegramLink:
    text: str
    url: str

    @property
    def markdown(self) -> str:
        label = self.text.strip() or self.url
        label = label.replace("[", "\\[").replace("]", "\\]")
        url = self.url.strip()
        if not url:
            return label
        if label == url:
            return url
        return f"[{label}]({url})"

    @property
    def plain(self) -> str:
        label = self.text.strip()
        url = self.url.strip()
        if not label or label == url:
            return url
        return f"{label}: {url}"


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
    links: list[TelegramLink] = field(default_factory=list)
    grouped_id: int | None = None
    media_paths: list[Path] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.media_path and not self.media_paths:
            self.media_paths.append(self.media_path)
        if self.media_type and not self.media_types:
            self.media_types.append(self.media_type)

    @property
    def sender_name(self) -> str:
        return self.sender_display_name or self.sender_username or str(self.sender_id or "unknown")

    @property
    def media_note(self) -> str:
        if not self.media_types and not self.media_type:
            return ""
        parts: list[str] = []
        media_types = self.media_types or ([self.media_type] if self.media_type else [])
        media_paths = self.media_paths or ([self.media_path] if self.media_path else [])
        for index, media_type in enumerate(media_types, start=1):
            location = ""
            if index <= len(media_paths) and media_paths[index - 1]:
                location = f" {media_paths[index - 1]}"
            parts.append(f"[media {index}: {media_type}{location}]")
        return "\n".join(parts)

    @property
    def links_note(self) -> str:
        if not self.links:
            return ""
        lines = ["链接："]
        lines.extend(f"- {link.markdown}" for link in self.links if link.url)
        return "\n".join(lines)

    @property
    def plain_links_note(self) -> str:
        if not self.links:
            return ""
        lines = ["链接："]
        lines.extend(f"- {link.plain}" for link in self.links if link.url)
        return "\n".join(lines)
