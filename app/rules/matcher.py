from __future__ import annotations

import logging
import re

from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule

logger = logging.getLogger(__name__)


class RuleMatcher:
    def matches(self, rule: ForwardRule, message: TelegramForwardMessage) -> bool:
        if not rule.enabled:
            return False

        if rule.source_chat_id is not None and rule.source_chat_id != message.chat_id:
            return False

        if rule.source_chat_type and rule.source_chat_type != message.chat_type:
            return False

        if rule.source_sender_id is not None and rule.source_sender_id != message.sender_id:
            return False

        if (
            rule.source_sender_is_bot is not None
            and rule.source_sender_is_bot != message.sender_is_bot
        ):
            return False

        if rule.media_types:
            message_media_type = message.media_type or "text"
            if message_media_type not in rule.media_types:
                return False

        if rule.text_include_regex:
            try:
                if not re.search(rule.text_include_regex, message.searchable_text):
                    return False
            except re.error:
                logger.warning("Invalid include regex in rule %s", rule.id)
                return False

        if rule.text_exclude_regex:
            try:
                if re.search(rule.text_exclude_regex, message.searchable_text):
                    return False
            except re.error:
                logger.warning("Invalid exclude regex in rule %s", rule.id)
                return False

        return True
