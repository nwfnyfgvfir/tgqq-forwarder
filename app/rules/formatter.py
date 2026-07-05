from __future__ import annotations

from app.rules.models import (
    TelegramForwardMessage,
    TelegramLink,
    extract_visible_url_keys,
    url_dedupe_key,
)
from app.storage.models import ForwardRule


class MessageFormatter:
    def format(self, rule: ForwardRule, message: TelegramForwardMessage) -> str:
        template = rule.message_template
        base_values = self._template_values(message, links_note="", plain_links_note="")
        base_rendered = self._render(template, base_values)
        links = self._links_for_output(message, base_rendered)
        links_note = self._links_note(links)
        values = self._template_values(
            message,
            links_note=links_note,
            plain_links_note=links_note,
        )
        rendered = self._render(template, values)
        return self._append_missing_links(rendered, message).strip()

    @staticmethod
    def _template_values(
        message: TelegramForwardMessage,
        *,
        links_note: str,
        plain_links_note: str,
    ) -> dict[str, object]:
        return {
            "message_id": message.message_id,
            "chat_id": message.chat_id or "",
            "chat_title": message.chat_title or "",
            "chat_type": message.chat_type,
            "sender_id": message.sender_id or "",
            "sender_username": message.sender_username or "",
            "sender_name": message.sender_name,
            "sender_is_bot": message.sender_is_bot,
            "text": message.text or "",
            "media_type": message.media_type or "",
            "media_path": str(message.media_path or ""),
            "media_note": message.media_note,
            "links_note": links_note,
            "plain_links_note": plain_links_note,
            "raw_url": message.raw_url or "",
        }

    @staticmethod
    def _render(template: str, values: dict[str, object]) -> str:
        try:
            rendered = template.format(**values)
        except Exception:
            rendered = (
                f"[Telegram: {values['chat_title']}]\n"
                f"{values['sender_name']}: {values['text']}\n"
                f"{values['links_note']}\n"
                f"{values['media_note']}"
            )
        return rendered.strip()

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
        return "\n".join(["链接：", *(f"- {link.plain}" for link in links)])

    @classmethod
    def _append_missing_links(cls, rendered: str, message: TelegramForwardMessage) -> str:
        if not message.links:
            return rendered
        missing = cls._links_for_output(message, rendered)
        if not missing:
            return rendered
        links_note = cls._links_note(missing)
        return f"{rendered}\n{links_note}" if rendered else links_note
