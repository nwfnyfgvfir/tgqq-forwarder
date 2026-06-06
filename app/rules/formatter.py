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
            "raw_url": message.raw_url or "",
        }
        try:
            return template.format(**values).strip()
        except Exception:
            return (
                f"[Telegram: {values['chat_title']}]\n"
                f"{values['sender_name']}: {values['text']}\n"
                f"{values['media_note']}"
            ).strip()
