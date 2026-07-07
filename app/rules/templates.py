from __future__ import annotations

OLD_DEFAULT_MESSAGE_TEMPLATE = (
    "[Telegram: {chat_title}]\n"
    "{sender_name}: {text}\n"
    "{links_note}\n"
    "{media_note}"
)

DEFAULT_MESSAGE_TEMPLATE = (
    "# {source_title_md}\n\n"
    "**发送者：{sender_name_md}**\n\n"
    "{text}\n\n"
    "{footer_note}"
)


def effective_message_template(template: str) -> str:
    if template.strip() == OLD_DEFAULT_MESSAGE_TEMPLATE.strip():
        return DEFAULT_MESSAGE_TEMPLATE
    return template


def templates_match(left: str, right: str) -> bool:
    return effective_message_template(left).strip() == effective_message_template(right).strip()
