from __future__ import annotations

from app.rules.formatter import MessageFormatter
from app.rules.matcher import RuleMatcher
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule


def make_message(**kwargs) -> TelegramForwardMessage:
    data = {
        "message_id": 1,
        "chat_id": -100123,
        "chat_title": "news",
        "chat_type": "channel",
        "sender_id": 42,
        "sender_username": "sender",
        "sender_display_name": "Sender",
        "sender_is_bot": False,
        "text": "hello world",
        "media_type": None,
        "media_path": None,
        "date": None,
    }
    data.update(kwargs)
    return TelegramForwardMessage(**data)


def test_rule_matcher_chat_sender_and_regex() -> None:
    rule = ForwardRule(
        name="r1",
        source_chat_id=-100123,
        source_chat_type="channel",
        source_sender_id=42,
        text_include_regex="hello",
        text_exclude_regex="blocked",
        qq_target_type="group",
        qq_target_id="target",
        message_template="{text}",
    )
    assert RuleMatcher().matches(rule, make_message())
    assert not RuleMatcher().matches(rule, make_message(text="blocked hello"))
    assert not RuleMatcher().matches(rule, make_message(chat_id=-100999))


def test_formatter_uses_template_values() -> None:
    rule = ForwardRule(
        name="r1",
        qq_target_type="group",
        qq_target_id="target",
        message_template="{chat_title}|{sender_name}|{text}",
    )
    assert MessageFormatter().format(rule, make_message()) == "news|Sender|hello world"
