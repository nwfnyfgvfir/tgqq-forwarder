from __future__ import annotations

from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule


class MessageFormatter:
    def format(self, rule: ForwardRule, message: TelegramForwardMessage) -> str:
        template = rule.message_template
        values = {
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
            "links_note": message.links_note,
            "plain_links_note": message.plain_links_note,
            "raw_url": message.raw_url or "",
        }
        try:
            rendered = template.format(**values).strip()
        except Exception:
            rendered = (
                f"[Telegram: {values['chat_title']}]\n"
                f"{values['sender_name']}: {values['text']}\n"
                f"{values['links_note']}\n"
                f"{values['media_note']}"
            ).strip()
        return self._append_missing_links(rendered, message).strip()

    @staticmethod
    def _append_missing_links(rendered: str, message: TelegramForwardMessage) -> str:
        if not message.links:
            return rendered
        missing = [link for link in message.links if link.url and link.url not in rendered]
        if not missing:
            return rendered
        links_note = "\n".join(["链接：", *(f"- {link.markdown}" for link in missing)])
        return f"{rendered}\n{links_note}" if rendered else links_note
