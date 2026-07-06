from __future__ import annotations

from app.rules.formatter import MessageFormatter
from app.rules.keywords import (
    keywords_from_text_include_regex,
    keywords_to_text_include_regex,
    split_keyword_args,
)
from app.rules.matcher import RuleMatcher
from app.rules.models import TelegramForwardMessage, TelegramLink
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


def make_rule(template: str = "{text}\n{links_note}") -> ForwardRule:
    return ForwardRule(
        name="r1",
        qq_target_type="group",
        qq_target_id="target",
        message_template=template,
    )


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


def test_rule_matcher_keyword_regex_matches_any_keyword_case_insensitive() -> None:
    rule = ForwardRule(
        name="r1",
        text_include_regex=keywords_to_text_include_regex(["AI", "Python", "机器人"]),
        qq_target_type="group",
        qq_target_id="target",
        message_template="{text}",
    )
    assert RuleMatcher().matches(rule, make_message(text="python news"))
    assert RuleMatcher().matches(rule, make_message(text="智能机器人发布"))
    assert not RuleMatcher().matches(rule, make_message(text="hello world"))


def test_keyword_helpers_split_dedupe_and_round_trip() -> None:
    keywords = split_keyword_args(["AI,Python", "机器人", "AI", "量化；交易"])
    assert keywords == ["AI", "Python", "机器人", "量化", "交易"]
    pattern = keywords_to_text_include_regex(keywords)
    assert keywords_from_text_include_regex(pattern) == keywords


def test_formatter_uses_template_values() -> None:
    rule = ForwardRule(
        name="r1",
        qq_target_type="group",
        qq_target_id="target",
        message_template="{chat_title}|{sender_name}|{text}",
    )
    assert MessageFormatter().format(rule, make_message()) == "news|Sender|hello world"


def test_formatter_highlights_keyword_rule_text() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = keywords_to_text_include_regex(["AI", "机器人"])
    message = make_message(text="AI news 智能机器人发布")

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "**AI** news 智能**机器人**发布"


def test_formatter_keyword_highlight_is_case_insensitive_and_preserves_casing() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = keywords_to_text_include_regex(["python"])
    message = make_message(text="PyThOn news")

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "**PyThOn** news"


def test_formatter_does_not_highlight_arbitrary_include_regex() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = "AI"
    message = make_message(text="AI news")

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "AI news"


def test_formatter_keyword_highlight_skips_visible_urls_and_preserves_dedupe() -> None:
    rule = make_rule("{text}\n{links_note}")
    rule.text_include_regex = keywords_to_text_include_regex(["AI"])
    message = make_message(
        text="AI https://example.com/AI",
        links=[
            TelegramLink(
                text="https://example.com/AI",
                url="https://example.com/AI",
                source="visible_url",
            )
        ],
    )

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "**AI** https://example.com/AI"
    assert "链接：" not in rendered


def test_formatter_keyword_highlight_does_not_touch_hidden_link_notes() -> None:
    rule = make_rule("{text}\n{links_note}")
    rule.text_include_regex = keywords_to_text_include_regex(["AI"])
    message = make_message(
        text="AI docs",
        links=[TelegramLink(text="AI Docs", url="https://example.com/ai", source="text_url")],
    )

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "**AI** docs\n链接：\n- AI Docs: https://example.com/ai"


def test_formatter_keyword_highlight_prefers_longer_overlapping_keywords() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = keywords_to_text_include_regex(["AI", "AIGC"])
    message = make_message(text="AIGC")

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "**AIGC**"


def test_formatter_does_not_duplicate_visible_url() -> None:
    message = make_message(
        text="read https://example.com",
        links=[
            TelegramLink(
                text="https://example.com",
                url="https://example.com",
                source="visible_url",
            )
        ],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert rendered.count("https://example.com") == 1
    assert "链接：" not in rendered


def test_formatter_preserves_hidden_text_url() -> None:
    message = make_message(
        text="read docs",
        links=[TelegramLink(text="Docs", url="https://example.com/docs", source="text_url")],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert "链接：" in rendered
    assert "- Docs: https://example.com/docs" in rendered


def test_formatter_preserves_button_url() -> None:
    message = make_message(
        text="button below",
        links=[TelegramLink(text="查看回复", url="https://example.com/reply", source="button_url")],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert "- 查看回复: https://example.com/reply" in rendered


def test_formatter_dedupes_visible_bare_domain_and_normalized_url() -> None:
    message = make_message(
        text="read example.com",
        links=[TelegramLink(text="Open", url="https://example.com", source="button_url")],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert "Open: https://example.com" not in rendered
    assert "链接：" not in rendered


def test_formatter_appends_hidden_links_when_template_omits_links_note() -> None:
    message = make_message(
        text="button below",
        links=[TelegramLink(text="Open", url="https://example.com/open", source="button_url")],
    )

    rendered = MessageFormatter().format(make_rule("{text}"), message)

    assert rendered == "button below\n链接：\n- Open: https://example.com/open"
