from __future__ import annotations

from app.rules.keywords import (
    detect_keyword_matches,
    highlight_keywords_for_markdown,
    is_keyword_text_include_regex,
    keywords_from_text_include_regex,
)
from app.rules.models import (
    TelegramForwardMessage,
    TelegramLink,
    extract_visible_url_keys,
    url_dedupe_key,
)
from app.rules.templates import DEFAULT_MESSAGE_TEMPLATE, effective_message_template
from app.storage.models import ForwardRule


class MessageFormatter:
    def format(self, rule: ForwardRule, message: TelegramForwardMessage) -> str:
        template = effective_message_template(rule.message_template)
        keywords = self._keywords_for_rule(rule)
        formatted_text = self._text_for_rule(message, keywords)
        keywords_note = self._keywords_note(
            detect_keyword_matches(message.searchable_text, keywords)
        )
        base_values = self._template_values(
            message,
            links_note="",
            plain_links_note="",
            keywords_note=keywords_note,
            text=formatted_text,
        )
        base_rendered = self._render(template, base_values)
        links = self._links_for_output(message, base_rendered)
        links_note = self._links_note(links)
        values = self._template_values(
            message,
            links_note=links_note,
            plain_links_note=self._plain_links_note(links),
            keywords_note=keywords_note,
            text=formatted_text,
        )
        rendered = self._render(template, values)
        return self._append_missing_tail(rendered, message, template, keywords_note).strip()

    @staticmethod
    def _keywords_for_rule(rule: ForwardRule) -> list[str]:
        if not is_keyword_text_include_regex(rule.text_include_regex):
            return []
        return keywords_from_text_include_regex(rule.text_include_regex)

    @classmethod
    def _text_for_rule(cls, message: TelegramForwardMessage, keywords: list[str]) -> str:
        text = cls._text_with_inline_links(message)
        text = cls._append_webpage_preview_text(text, message)
        if not keywords:
            return text
        return highlight_keywords_for_markdown(text, keywords)

    @classmethod
    def _append_webpage_preview_text(cls, text: str, message: TelegramForwardMessage) -> str:
        preview_lines = cls._deduped_webpage_preview_lines(text, message)
        if not preview_lines:
            return text
        preview = "\n".join(["链接预览：", *preview_lines])
        if text.strip():
            return f"{text.rstrip()}\n{preview}"
        return preview

    @staticmethod
    def _deduped_webpage_preview_lines(
        text: str,
        message: TelegramForwardMessage,
    ) -> list[str]:
        existing = text or ""
        lines: list[str] = []
        for value in (message.webpage_title, message.webpage_description):
            line = (value or "").strip()
            if not line:
                continue
            if line in existing or line in lines:
                continue
            lines.append(line)
        return lines

    @classmethod
    def _text_with_inline_links(cls, message: TelegramForwardMessage) -> str:
        text = message.text or ""
        spans: list[tuple[int, int, TelegramLink]] = []
        emitted: set[str] = set()
        for link in message.links:
            key = url_dedupe_key(link.url)
            if not key or key in emitted:
                continue
            span = cls._link_span(text, link)
            if span is None:
                continue
            start, end = span
            overlaps_existing = any(
                start < existing_end and end > existing_start
                for existing_start, existing_end, _ in spans
            )
            if overlaps_existing:
                continue
            emitted.add(key)
            spans.append((start, end, link))

        for start, end, link in sorted(spans, key=lambda item: item[0], reverse=True):
            text = f"{text[:start]}{link.markdown}{text[end:]}"
        return text

    @classmethod
    def _link_span(cls, text: str, link: TelegramLink) -> tuple[int, int] | None:
        if link.source == "text_url":
            if link.text_start is not None and link.text_end is not None:
                if 0 <= link.text_start < link.text_end <= len(text):
                    if text[link.text_start : link.text_end] == link.text:
                        return link.text_start, link.text_end
            return cls._unique_label_span(text, link.text)
        if link.source == "button_url":
            return cls._unique_label_span(text, link.text)
        return None

    @staticmethod
    def _unique_label_span(text: str, label: str) -> tuple[int, int] | None:
        label = label.strip()
        if not text or not label:
            return None
        first = text.find(label)
        if first < 0:
            return None
        second = text.find(label, first + len(label))
        if second >= 0:
            return None
        return first, first + len(label)

    @classmethod
    def _template_values(
        cls,
        message: TelegramForwardMessage,
        *,
        links_note: str,
        plain_links_note: str,
        keywords_note: str,
        text: str | None = None,
    ) -> dict[str, object]:
        footer_note = cls._footer_note(links_note, message.media_note, keywords_note)
        source_title = cls._source_title(message)
        return {
            "message_id": message.message_id,
            "chat_id": message.chat_id or "",
            "chat_title": message.chat_title or "",
            "chat_type": message.chat_type,
            "source_title": source_title,
            "source_title_md": cls._escape_markdown_text(source_title),
            "sender_id": message.sender_id or "",
            "sender_username": message.sender_username or "",
            "sender_name": message.sender_name,
            "sender_name_md": cls._escape_markdown_text(message.sender_name),
            "sender_is_bot": message.sender_is_bot,
            "text": text if text is not None else message.text or "",
            "webpage_title": message.webpage_title or "",
            "webpage_description": message.webpage_description or "",
            "webpage_url": message.webpage_url or "",
            "webpage_preview_text": message.webpage_preview_text,
            "media_type": message.media_type or "",
            "media_path": "",
            "media_note": message.media_note,
            "links_note": links_note,
            "plain_links_note": plain_links_note,
            "keywords_note": keywords_note,
            "footer_note": footer_note,
            "raw_url": message.raw_url or "",
        }

    @staticmethod
    def _render(template: str, values: dict[str, object]) -> str:
        try:
            rendered = template.format(**values)
        except Exception:
            rendered = DEFAULT_MESSAGE_TEMPLATE.format(**values)
        return rendered.strip()

    @staticmethod
    def _source_title(message: TelegramForwardMessage) -> str:
        if message.chat_title:
            return f"Telegram：{message.chat_title}"
        if message.chat_id is not None:
            return f"Telegram：{message.chat_id}"
        return "Telegram：unknown"

    @staticmethod
    def _escape_markdown_text(value: object) -> str:
        text = str(value)
        for char in ("\\", "[", "]", "*", "_", "`"):
            text = text.replace(char, f"\\{char}")
        return text

    @staticmethod
    def _footer_note(*parts: str) -> str:
        return "\n".join(part.strip() for part in parts if part and part.strip())

    @staticmethod
    def _keywords_note(keywords: list[str]) -> str:
        if not keywords:
            return ""
        return f"检测到关键词：{'、'.join(keywords)}"

    @classmethod
    def _links_for_output(
        cls,
        message: TelegramForwardMessage,
        rendered_context: str,
    ) -> list[TelegramLink]:
        visible_url_keys = extract_visible_url_keys(rendered_context)
        links: list[TelegramLink] = []
        emitted: set[str] = set()
        for link in message.links:
            key = url_dedupe_key(link.url)
            if not key:
                continue
            if link.source == "visible_url":
                continue
            if key in visible_url_keys or key in emitted:
                continue
            emitted.add(key)
            links.append(link)
        return links

    @staticmethod
    def _links_note(links: list[TelegramLink]) -> str:
        if not links:
            return ""
        return "\n".join(["相关链接：", *(f"- {link.markdown}" for link in links)])

    @staticmethod
    def _plain_links_note(links: list[TelegramLink]) -> str:
        if not links:
            return ""
        return "\n".join(["相关链接：", *(f"- {link.plain}" for link in links)])

    @classmethod
    def _append_missing_tail(
        cls,
        rendered: str,
        message: TelegramForwardMessage,
        template: str,
        keywords_note: str,
    ) -> str:
        tail_parts: list[str] = []
        template_has_links = cls._template_contains_any(
            template,
            "links_note",
            "plain_links_note",
            "footer_note",
        )
        if not template_has_links:
            missing = cls._links_for_output(message, rendered)
            links_note = cls._links_note(missing)
            if links_note:
                tail_parts.append(links_note)
        template_has_keywords = cls._template_contains_any(template, "keywords_note", "footer_note")
        if keywords_note and not template_has_keywords:
            tail_parts.append(keywords_note)
        tail = cls._footer_note(*tail_parts)
        if not tail:
            return rendered
        return f"{rendered}\n{tail}" if rendered else tail

    @staticmethod
    def _template_contains_any(template: str, *names: str) -> bool:
        return any(f"{{{name}}}" in template for name in names)
