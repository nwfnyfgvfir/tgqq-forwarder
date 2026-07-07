from __future__ import annotations

from pathlib import Path

from app.rules.formatter import MessageFormatter
from app.rules.keywords import (
    detect_keyword_matches,
    keywords_from_text_include_regex,
    keywords_to_text_include_regex,
    split_keyword_args,
)
from app.rules.matcher import RuleMatcher
from app.rules.models import TelegramForwardMessage, TelegramLink
from app.rules.templates import OLD_DEFAULT_MESSAGE_TEMPLATE
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


def test_rule_matcher_uses_webpage_preview_text_for_regexes() -> None:
    rule = ForwardRule(
        name="r1",
        text_include_regex="独服",
        text_exclude_regex="已售出",
        qq_target_type="group",
        qq_target_id="target",
        message_template="{text}",
    )

    assert RuleMatcher().matches(
        rule,
        make_message(
            text="标题",
            webpage_title="NodeSeek",
            webpage_description="代价配置私我看老们都在买阿三的独服",
        ),
    )
    assert not RuleMatcher().matches(
        rule,
        make_message(
            text="标题",
            webpage_title="NodeSeek",
            webpage_description="独服已售出",
        ),
    )


def test_keyword_helpers_split_dedupe_and_round_trip() -> None:
    keywords = split_keyword_args(["AI Python", "机器人", "AI", "量化；交易\n行情"])
    assert keywords == ["AI", "Python", "机器人", "量化", "交易", "行情"]
    pattern = keywords_to_text_include_regex(keywords)
    assert keywords_from_text_include_regex(pattern) == keywords


def test_detect_keyword_matches_returns_actual_hits_and_skips_visible_urls() -> None:
    keywords = ["AI", "Python"]

    assert detect_keyword_matches("read https://example.com/AI Python", keywords) == ["Python"]


def test_formatter_uses_template_values() -> None:
    rule = ForwardRule(
        name="r1",
        qq_target_type="group",
        qq_target_id="target",
        message_template="{chat_title}|{sender_name}|{text}",
    )
    assert MessageFormatter().format(rule, make_message()) == "news|Sender|hello world"


def test_formatter_exposes_webpage_preview_template_values() -> None:
    rule = make_rule("{webpage_title}|{webpage_description}|{webpage_url}|{webpage_preview_text}")
    message = make_message(
        webpage_title="标题",
        webpage_description="摘要",
        webpage_url="https://example.com",
    )

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "标题|摘要|https://example.com|标题\n摘要"


def test_formatter_auto_upgrades_old_default_template() -> None:
    rule = make_rule(OLD_DEFAULT_MESSAGE_TEMPLATE)
    rule.text_include_regex = keywords_to_text_include_regex(["hello"])

    rendered = MessageFormatter().format(rule, make_message(media_type="photo"))

    assert rendered == (
        "# Telegram：news\n\n"
        "**发送者：Sender**\n\n"
        "***hello*** world\n\n"
        "媒体：photo\n"
        "检测到关键词：hello"
    )


def test_formatter_keeps_custom_template_shape() -> None:
    rendered = MessageFormatter().format(make_rule("{sender_name}: {text}"), make_message())

    assert rendered == "Sender: hello world"


def test_formatter_highlights_keyword_rule_text_and_appends_keyword_note() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = keywords_to_text_include_regex(["AI", "机器人"])
    message = make_message(text="AI news 智能机器人发布")

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "***AI*** news 智能***机器人***发布\n检测到关键词：AI、机器人"


def test_formatter_appends_webpage_preview_without_repeating_title() -> None:
    message = make_message(
        text="NodeSeek 官方频道",
        webpage_title="NodeSeek 官方频道",
        webpage_description="代价配置私我看老们都在买阿三的独服",
    )

    rendered = MessageFormatter().format(make_rule("{text}"), message)

    assert rendered == "NodeSeek 官方频道\n代价配置私我看老们都在买阿三的独服"
    assert "链接预览：" not in rendered


def test_formatter_keywords_include_webpage_preview_text() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = keywords_to_text_include_regex(["独服"])
    message = make_message(
        text="NodeSeek 官方频道",
        webpage_title="NodeSeek 官方频道",
        webpage_description="代价配置私我看老们都在买阿三的独服",
    )

    rendered = MessageFormatter().format(rule, message)

    assert rendered == (
        "NodeSeek 官方频道\n"
        "代价配置私我看老们都在买阿三的***独服***\n"
        "检测到关键词：独服"
    )
    assert "链接预览：" not in rendered


def test_formatter_keyword_highlight_is_case_insensitive_and_preserves_casing() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = keywords_to_text_include_regex(["python"])
    message = make_message(text="PyThOn news")

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "***PyThOn*** news\n检测到关键词：python"


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

    assert rendered == "***AI*** https://example.com/AI\n检测到关键词：AI"
    assert "相关链接：" not in rendered


def test_formatter_keyword_highlight_does_not_touch_hidden_link_notes() -> None:
    rule = make_rule("{text}\n{links_note}")
    rule.text_include_regex = keywords_to_text_include_regex(["AI"])
    message = make_message(
        text="AI docs",
        links=[TelegramLink(text="AI Docs", url="https://example.com/ai", source="text_url")],
    )

    rendered = MessageFormatter().format(rule, message)

    assert rendered == (
        "***AI*** docs\n"
        "- [AI Docs](https://example.com/ai)\n"
        "检测到关键词：AI"
    )
    assert "相关链接：" not in rendered


def test_formatter_keyword_highlight_prefers_longer_overlapping_keywords() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = keywords_to_text_include_regex(["AI", "AIGC"])
    message = make_message(text="AIGC")

    rendered = MessageFormatter().format(rule, message)

    assert rendered == "***AIGC***\n检测到关键词：AIGC"


def test_formatter_inlines_hidden_text_url_with_valid_offsets() -> None:
    message = make_message(
        text="read Docs now",
        links=[
            TelegramLink(
                text="Docs",
                url="https://example.com/docs",
                source="text_url",
                text_start=5,
                text_end=9,
            )
        ],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert rendered == "read [Docs](https://example.com/docs) now"
    assert "相关链接：" not in rendered


def test_formatter_inlines_hidden_text_url_without_offsets_when_label_is_unique() -> None:
    message = make_message(
        text="read Docs now",
        links=[TelegramLink(text="Docs", url="https://example.com/docs", source="text_url")],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert rendered == "read [Docs](https://example.com/docs) now"


def test_formatter_hidden_text_url_with_invalid_offset_falls_back_to_link_note() -> None:
    message = make_message(
        text="Docs Docs",
        links=[
            TelegramLink(
                text="Docs",
                url="https://example.com/docs",
                source="text_url",
                text_start=2,
                text_end=6,
            )
        ],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert rendered == "Docs Docs\n- [Docs](https://example.com/docs)"
    assert "相关链接：" not in rendered


def test_formatter_inlines_button_url_when_label_is_unique() -> None:
    message = make_message(
        text="点击查看回复",
        links=[TelegramLink(text="查看回复", url="https://example.com/reply", source="button_url")],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert rendered == "点击[查看回复](https://example.com/reply)"
    assert "按钮" not in rendered


def test_formatter_button_url_note_has_no_button_prefix() -> None:
    message = make_message(
        text="button below",
        links=[TelegramLink(text="查看回复", url="https://example.com/reply", source="button_url")],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert rendered == "button below\n- [查看回复](https://example.com/reply)"
    assert "相关链接：" not in rendered
    assert "按钮" not in rendered


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
    assert "相关链接：" not in rendered


def test_formatter_dedupes_visible_bare_domain_and_normalized_url() -> None:
    message = make_message(
        text="read example.com",
        links=[TelegramLink(text="Open", url="https://example.com", source="button_url")],
    )

    rendered = MessageFormatter().format(make_rule(), message)

    assert "Open" not in rendered
    assert "https://example.com" not in rendered
    assert "相关链接：" not in rendered


def test_formatter_appends_hidden_links_when_template_omits_links_note() -> None:
    message = make_message(
        text="button below",
        links=[TelegramLink(text="Open", url="https://example.com/open", source="button_url")],
    )

    rendered = MessageFormatter().format(make_rule("{text}"), message)

    assert rendered == "button below\n- [Open](https://example.com/open)"
    assert "相关链接：" not in rendered


def test_formatter_media_note_omits_paths_and_media_path_variable_is_empty() -> None:
    message = make_message(
        media_type="photo",
        media_path=Path("/tmp/secret/photo.jpg"),
        media_paths=[Path("/tmp/secret/photo.jpg"), Path("/tmp/secret/video.mp4")],
        media_types=["photo", "video"],
    )

    rendered = MessageFormatter().format(make_rule("{media_path}|{media_note}"), message)

    assert rendered == "|媒体：photo、video"
    assert "/tmp/secret" not in rendered
    assert "photo.jpg" not in rendered


def test_formatter_keyword_note_is_last_when_links_are_missing_from_template() -> None:
    rule = make_rule("{text}")
    rule.text_include_regex = keywords_to_text_include_regex(["AI"])
    message = make_message(
        text="AI docs",
        links=[TelegramLink(text="Docs", url="https://example.com/docs", source="button_url")],
    )

    rendered = MessageFormatter().format(rule, message)

    assert rendered.endswith("检测到关键词：AI")
    assert rendered == (
        "***AI*** docs\n"
        "- [Docs](https://example.com/docs)\n"
        "检测到关键词：AI"
    )
    assert "相关链接：" not in rendered
