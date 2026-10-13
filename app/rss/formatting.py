from __future__ import annotations

from html.parser import HTMLParser

from app.rss.errors import RssError
from app.rss.schemas import validate_template


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag in {"script", "style"}:
            self.hidden.append(tag)
        elif not self.hidden and tag in {"br", "p", "div", "li", "hr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def matches_keywords(config, entry) -> bool:
    text = f"{entry.title}\n{entry.summary}".casefold()
    includes = config.include_keywords
    excludes = config.exclude_keywords
    return (not includes or any(k.casefold() in text for k in includes)) and not any(
        k.casefold() in text for k in excludes
    )


def render_entry(config, entry, feed_title: str | None = None) -> str:
    validate_template(config.message_template)
    published = entry.published_at.isoformat() if entry.published_at else ""
    text = config.message_template.format(
        subscription_name=config.name,
        feed_title=feed_title if feed_title is not None else entry.feed_title,
        title=entry.title,
        summary=entry.summary[: config.max_summary_length],
        link=entry.link,
        published_at=published,
    ).strip()
    if entry.link and entry.link not in text:
        text = f"{text}\n\n{entry.link}".strip()
    return fit_message(text, entry.link)


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _cut(text: str, budget: int) -> str:
    # 按完整 Unicode 码点截断，避免拆开 emoji 的 UTF-16 surrogate pair。
    return text.encode("utf-16-le")[: budget * 2].decode("utf-16-le", errors="ignore")


def fit_message(text: str, link: str = "", limit: int = 4096) -> str:
    if link and utf16_units(link) > limit:
        raise RssError("rss_link_too_long", 422, "原文链接超过 TG 单条消息长度限制")
    if utf16_units(text) <= limit and (not link or link in text):
        return text
    if not link:
        return _cut(text, limit)
    body = text.replace(link, "").strip()
    remaining = limit - utf16_units(link)
    if remaining <= 2:
        return link
    body = _cut(body, remaining - 2).rstrip()
    return f"{body}\n\n{link}" if body else link
