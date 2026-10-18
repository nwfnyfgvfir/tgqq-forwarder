from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from string import Formatter

from app.rss.errors import RssError
from app.rss.schemas import template_fields, validate_template

BLANK_LINES = re.compile(r"\n{3,}")
PARAGRAPHS = re.compile(r"\n{2,}")
TRANSLATED_FIELDS = ("title", "summary")


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


def format_published_at(value: datetime | None) -> str:
    """把条目发布时间渲染为本地可读文本；缺失时返回空串，由空白行折叠处理。"""
    if value is None:
        return ""
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def translated_fields(template: str) -> tuple[str, ...]:
    """模板引用了哪些原文字段，就只翻译哪些字段。

    模板没写 `{title}` 时不翻译标题，`{translation}` 里也不会出现译文标题；
    若模板只用 `{translation}`（原文字段全由译文承担），标题与摘要都翻译。
    """
    used = template_fields(template)
    picked = tuple(name for name in TRANSLATED_FIELDS if name in used)
    return picked or TRANSLATED_FIELDS


def build_translation_block(entry, template: str | None = None) -> str:
    """原文在上、译文在下：有译文标题/摘要时按「译文标题\\n\\n译文摘要」拼接。"""
    wanted = TRANSLATED_FIELDS if template is None else translated_fields(template)
    values = {
        "title": getattr(entry, "translated_title", None),
        "summary": getattr(entry, "translated_summary", None),
    }
    parts = []
    for name in wanted:
        value = values[name]
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts)


def _only_empty_fields(line: str, values: dict[str, str]) -> bool:
    """该行是否引用了占位符、且这些占位符渲染后全为空白。"""
    fields = [name for _, name, _, _ in Formatter().parse(line) if name]
    return bool(fields) and all(not (values.get(name) or "").strip() for name in fields)


def drop_empty_blocks(template: str, values: dict[str, str]) -> str:
    """占位符全空的行/段整体省略，让可选区块连同图标、分隔线一起消失。

    段落按空行划分。段内占位符全空 → 整段丢掉（`┈┈┈` + `🌐 译文` 这类固定装饰
    跟着译文一起走）；否则逐行判断，某行引用的占位符全空 → 只删这一行
    （`🕒 {published_at}` 与订阅名同段，缺时间时不能把订阅名一起删掉）。
    段内只要还有有值的占位符，就交给 `format` 替换，空占位符变成空串。
    """
    kept: list[str] = []
    for paragraph in PARAGRAPHS.split(template):
        if _only_empty_fields(paragraph, values):
            continue
        lines = [line for line in paragraph.split("\n") if not _only_empty_fields(line, values)]
        if lines:
            kept.append("\n".join(lines))
    return "\n\n".join(kept)


def render_entry(config, entry, feed_title: str | None = None) -> str:
    validate_template(config.message_template)
    values = {
        "subscription_name": config.name,
        "feed_title": feed_title if feed_title is not None else entry.feed_title,
        "title": entry.title,
        "summary": entry.summary[: config.max_summary_length],
        "link": entry.link,
        "published_at": format_published_at(entry.published_at),
        "translation": build_translation_block(entry, config.message_template),
    }
    template = drop_empty_blocks(config.message_template, values)
    text = template.format(**values).strip()
    # 行内部分占位符为空（如 `标题 · {published_at}`）时不留行尾空白。
    text = "\n".join(line.rstrip() for line in text.splitlines())
    if entry.link and entry.link not in text:
        text = f"{text}\n\n{entry.link}".strip()
    # 占位符为空（如无发布时间）时不留空行。
    return fit_message(BLANK_LINES.sub("\n\n", text), entry.link)


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
