from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.platforms import Platform

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
    """统一的来源消息模型（Telegram / 微信共用）。

    类名保留 ``Telegram`` 前缀是为了兼容既有调用方；实际由 ``source_platform``
    标明来源平台。``chat_id`` / ``sender_id`` 在 Telegram 下是 int，
    在个人微信下是 wxid 一类的字符串。
    """

    message_id: int | str
    chat_id: int | str | None
    chat_title: str | None
    chat_type: str
    sender_id: int | str | None
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
    # 与 media_paths 一一对应的原始文件名（拿不到时为 None），投递时用于还原文件名。
    media_filenames: list[str] = field(default_factory=list)
    account_id: str | None = None
    account_user_id: int | None = None
    account_username: str | None = None
    source_platform: str = Platform.TELEGRAM.value

    def __post_init__(self) -> None:
        if self.media_path and not self.media_paths:
            self.media_paths.append(self.media_path)
        if self.media_type and not self.media_types:
            self.media_types.append(self.media_type)

    @property
    def sender_name(self) -> str:
        return self.sender_display_name or self.sender_username or str(self.sender_id or "unknown")

    def media_filename_at(self, index: int) -> str | None:
        """第 ``index`` 个媒体的原始文件名；没有则返回 None（由调用方回退到本地文件名）。"""
        if 0 <= index < len(self.media_filenames):
            name = (self.media_filenames[index] or "").strip()
            return name or None
        return None

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


def id_key(value: object) -> str | None:
    """把 int/str 形式的会话或发送者 ID 归一化成字符串，便于跨平台比较。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
