from __future__ import annotations

# Oldest plain-text default (pre-markdown).
OLD_DEFAULT_MESSAGE_TEMPLATE = (
    "[Telegram: {chat_title}]\n"
    "{sender_name}: {text}\n"
    "{links_note}\n"
    "{media_note}"
)

# Previous markdown default without account line (still stored on some rules).
PREVIOUS_DEFAULT_MESSAGE_TEMPLATE = (
    "# {source_title_md}\n\n"
    "**发送者：{sender_name_md}**\n\n"
    "{text}\n\n"
    "{footer_note}"
)

DEFAULT_MESSAGE_TEMPLATE = (
    "# {source_title_md}\n\n"
    "账号：`{account_id}`\n\n"
    "**发送者：{sender_name_md}**\n\n"
    "{text}\n\n"
    "{footer_note}"
)

LEGACY_DEFAULT_MESSAGE_TEMPLATES = (
    OLD_DEFAULT_MESSAGE_TEMPLATE,
    PREVIOUS_DEFAULT_MESSAGE_TEMPLATE,
)


def effective_message_template(template: str) -> str:
    stripped = template.strip()
    if any(stripped == legacy.strip() for legacy in LEGACY_DEFAULT_MESSAGE_TEMPLATES):
        return DEFAULT_MESSAGE_TEMPLATE
    return template


def templates_match(left: str, right: str) -> bool:
    return effective_message_template(left).strip() == effective_message_template(right).strip()
