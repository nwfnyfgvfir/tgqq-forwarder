from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_VISIBLE_URL_RE = re.compile(
    r"""
    (?P<url>
        (?:[a-z][a-z0-9+.-]*://|mailto:|tel:|tg:)[^\s<>()\[\]{}"']+
        |
        (?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}
        (?::\d+)?
        (?:/[^\s<>()\[\]{}"']*)?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_TRAILING_URL_PUNCTUATION = ".,!?;:，。！？；：、)]}）】》"


def normalize_telegram_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if "://" in url or parts.scheme in {"mailto", "tel", "tg"}:
        return url
    return f"https://{url}"


def url_dedupe_key(url: str) -> str:
    normalized = normalize_telegram_url(url)
    if not normalized:
        return ""

    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return normalized.casefold()

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    default_port = {"http": ":80", "https": ":443"}.get(scheme)
    if default_port and netloc.endswith(default_port):
        netloc = netloc[: -len(default_port)]

    path = parts.path
    if path == "/" and not parts.query and not parts.fragment:
        path = ""
    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


def iter_visible_url_spans(text: str) -> Iterator[tuple[int, int]]:
    for match in _VISIBLE_URL_RE.finditer(text or ""):
        url = match.group("url").rstrip(_TRAILING_URL_PUNCTUATION)
        if not url:
            continue
        yield match.start("url"), match.start("url") + len(url)


def extract_visible_url_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for start, end in iter_visible_url_spans(text):
        key = url_dedupe_key(text[start:end])
        if key:
            keys.add(key)
    return keys


@dataclass(slots=True)
class TelegramLink:
    text: str
    url: str
    source: str = "unknown"
    text_start: int | None = None
    text_end: int | None = None

    @property
    def markdown(self) -> str:
        label = self.text.strip() or self.url
        label = (
            label.replace("\\", "\\\\")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace("`", "\\`")
        )
        url = self.url.strip().replace(")", "\\)")
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
    webpage_title: str | None = None
    webpage_description: str | None = None
    webpage_url: str | None = None
    links: list[TelegramLink] = field(default_factory=list)
    grouped_id: int | None = None
    media_paths: list[Path] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    account_id: str | None = None
    account_user_id: int | None = None
    account_username: str | None = None

    def __post_init__(self) -> None:
        if self.media_path and not self.media_paths:
            self.media_paths.append(self.media_path)
        if self.media_type and not self.media_types:
            self.media_types.append(self.media_type)

    @property
    def sender_name(self) -> str:
        return self.sender_display_name or self.sender_username or str(self.sender_id or "unknown")

    @property
    def webpage_preview_text(self) -> str:
        parts = [self.webpage_title, self.webpage_description]
        return "\n".join(part.strip() for part in parts if part and part.strip())

    @property
    def searchable_text(self) -> str:
        parts = [self.text, self.webpage_title, self.webpage_description]
        return "\n".join(part.strip() for part in parts if part and part.strip())

    @property
    def media_note(self) -> str:
        return ""

    @property
    def links_note(self) -> str:
        if not self.links:
            return ""
        lines = ["链接："]
        lines.extend(f"- {link.plain}" for link in self.links if link.url)
        return "\n".join(lines)

    @property
    def plain_links_note(self) -> str:
        if not self.links:
            return ""
        lines = ["链接："]
        lines.extend(f"- {link.plain}" for link in self.links if link.url)
        return "\n".join(lines)
