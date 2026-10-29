from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.rss.errors import RssError
from app.rss.formatting import (
    fit_message,
    format_published_at,
    matches_keywords,
    plain_text,
    render_entry,
    utf16_units,
)
from app.rss.schemas import SubscriptionInput


def payload(**kwargs) -> SubscriptionInput:
    return SubscriptionInput(
        **{
            "name": " rss ",
            "feed_url": "https://example.com/feed",
            "tg_chat_id": " -100123 ",
            **kwargs,
        }
    )


def test_defaults_and_normalization() -> None:
    config = payload(include_keywords=[" A ", "", "A"])
    assert config.name == "rss"
    assert config.tg_chat_id == "-100123"
    assert config.include_keywords == ["A"]
    assert config.enabled and config.interval_seconds == 600 and config.initial_policy == "skip"
    assert config.initial_count == 3 and config.max_summary_length == 600
    assert payload(tg_chat_id=" @channel_name ").tg_chat_id == "@channel_name"
    assert payload().exclude_keywords == []


@pytest.mark.parametrize("template", ["", " ", "\n\t ", "a" * 8001])
def test_template_payload_limits(template) -> None:
    with pytest.raises(ValidationError):
        payload(message_template=template)


def test_template_limit_accepts_boundary_and_preserves_layout() -> None:
    assert len(payload(message_template="a" * 8000).message_template) == 8000
    assert payload(message_template=" \n{title}\n ").message_template == " \n{title}\n "


@pytest.mark.parametrize("field", ["include_keywords", "exclude_keywords"])
@pytest.mark.parametrize(
    "words", [["a"] * 101, ["x" * 121], [" " * 121], [str(n) for n in range(101)]]
)
def test_keyword_payload_limits(field, words) -> None:
    with pytest.raises(ValidationError):
        payload(**{field: words})


@pytest.mark.parametrize("field", ["include_keywords", "exclude_keywords"])
def test_keyword_boundaries_strip_and_unique(field) -> None:
    words = [str(n) for n in range(99)] + ["x" * 120]
    assert getattr(payload(**{field: words}), field) == words
    assert getattr(payload(**{field: [" A ", "A", "", " B "]}), field) == ["A", "B"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/rss",
        "ftp://example.com/a",
        "https://u:p@example.com/rss",
        "https://example.com/rss#x",
        "https://example.com/rss#",
        "https:///rss",
        "https://example.com:bad/rss",
        "https://example.com:65536/rss",
        "https://example.com/\nsecret",
        "https://example.com\\@localhost/rss",
        "https://example.com/" + "a" * 2048,
    ],
)
def test_invalid_url(url) -> None:
    with pytest.raises(ValidationError):
        payload(feed_url=url)


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": " "},
        {"name": "a" * 121},
        {"tg_chat_id": "123"},
        {"tg_chat_id": "0"},
        {"tg_chat_id": "-0"},
        {"tg_chat_id": "@ab"},
        {"tg_chat_id": "@a/bbb"},
        {"interval_seconds": 59},
        {"interval_seconds": 86401},
        {"initial_count": 0},
        {"initial_count": 21},
        {"initial_policy": "all"},
        {"max_summary_length": 99},
        {"max_summary_length": 2001},
        {"unknown": True},
        {"include_keywords": [1]},
    ],
)
def test_invalid_configuration(overrides) -> None:
    with pytest.raises(ValidationError):
        payload(**overrides)


@pytest.mark.parametrize(
    "template",
    [
        "{unknown}",
        "{title.__class__}",
        "{title[0]}",
        "{title!r}",
        "{title:>2}",
        "{title:}",
        "{{{title:}}}",
        "{summary:{title}}",
        "{}",
        "{",
        "}",
        "{0}",
    ],
)
def test_template_rejects_non_simple_fields(template) -> None:
    with pytest.raises(ValidationError):
        payload(message_template=template)


def test_plain_template_and_original_link() -> None:
    entry = SimpleNamespace(
        title="<b>不是格式化标记</b>",
        summary="安全摘要",
        link="https://example.com/article",
        feed_title="feed",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    text = render_entry(payload(message_template="{{{title}}} {published_at}"), entry)
    # 发布时间渲染为本地时间，格式为 YYYY-MM-DD HH:MM。
    first_line = text.split("\n")[0]
    assert re.fullmatch(r"\{<b>不是格式化标记</b>\} \d{4}-\d{2}-\d{2} \d{2}:\d{2}", first_line)
    assert text.endswith(entry.link)
    assert render_entry(payload(message_template="{link}"), entry).count(entry.link) == 1
    assert "script" not in plain_text("<p>A &amp; B</p><script>x()</script><style>x</style>")
    assert plain_text("<p>A &amp; B</p><script>x()</script><style>x</style>") == "A & B"


def test_default_template_shows_publish_time_and_collapses_missing_one() -> None:
    entry = SimpleNamespace(
        title="每周分享",
        summary="摘要",
        link="https://example.com/1",
        feed_title="weekly",
        published_at=datetime(2026, 10, 13, 0, 30, tzinfo=UTC),
    )
    text = render_entry(payload(), entry)
    lines = text.splitlines()
    assert set(lines[0]) == {"━"}
    assert lines[1] == "📡 rss"
    assert lines[2] == f"🕒 {format_published_at(entry.published_at)}"
    assert "\n\n\n" not in text

    without_time = SimpleNamespace(**{**entry.__dict__, "published_at": None})
    blank = render_entry(payload(), without_time)
    # 缺发布时间时整行消失，不留下孤零零的 🕒。
    assert blank.splitlines()[0] == lines[0]
    assert blank.splitlines()[1] == "📡 rss"
    assert blank.splitlines()[2] == ""
    assert blank.splitlines()[3] == "# 每周分享"
    assert "\n\n\n" not in blank
    assert format_published_at(None) == ""


def test_default_template_keeps_clean_hierarchy_without_optional_fields() -> None:
    entry = SimpleNamespace(
        title="标题",
        summary="摘要",
        link="https://example.com/1",
        feed_title="feed",
        published_at=None,
        translated_title=None,
        translated_summary=None,
    )
    # 分隔线 → 元信息（📡 订阅名 / 🕒 时间）→ # 标题 → 正文 → 译文区 → 🔗 链接。
    assert render_entry(payload(), entry).splitlines() == [
        "━━━━━━━━━━━━━━",
        "📡 rss",
        "",
        "# 标题",
        "",
        "摘要",
        "",
        "🔗 https://example.com/1",
    ]
    translated = SimpleNamespace(
        **{**entry.__dict__, "translated_title": "译文标题", "translated_summary": "译文摘要"}
    )
    # 译文区带细线与 🌐 标签，与原文正文明显分开。
    assert render_entry(payload(), translated).splitlines() == [
        "━━━━━━━━━━━━━━",
        "📡 rss",
        "",
        "# 标题",
        "",
        "摘要",
        "",
        "┈┈┈┈┈┈┈┈┈┈┈┈┈┈",
        "🌐 译文",
        "译文标题",
        "",
        "译文摘要",
        "",
        "🔗 https://example.com/1",
    ]


def test_template_drops_optional_lines_and_blocks_when_empty() -> None:
    template = (
        "📡 {subscription_name}\n"
        "🕒 {published_at}\n"
        "\n"
        "# {title}\n"
        "\n"
        "┈┈┈\n"
        "🌐 译文\n"
        "{translation}\n"
        "\n"
        "🔗 {link}"
    )
    entry = SimpleNamespace(
        title="标题",
        summary="摘要",
        link="https://example.com/1",
        feed_title="feed",
        published_at=None,
        translated_title=None,
        translated_summary=None,
    )
    # 空占位符所在的行（🕒）与整段（细线 + 🌐 译文）一起消失，装饰不留残迹。
    assert render_entry(payload(message_template=template), entry).splitlines() == [
        "📡 rss",
        "",
        "# 标题",
        "",
        "🔗 https://example.com/1",
    ]


def test_template_escaped_braces_do_not_count_as_empty_fields() -> None:
    entry = SimpleNamespace(
        title="",
        summary="摘要",
        link="https://example.com/1",
        feed_title="feed",
        published_at=None,
    )
    text = render_entry(payload(message_template="{{title}} {summary}"), entry)
    assert text.splitlines()[0] == "{title} 摘要"


def test_emoji_utf16_budget_preserves_link() -> None:
    emoji = chr(0x1F642)
    link = "https://example.com/" + "a" * 2000
    text = fit_message(emoji * 5000 + "\n" + link, link)
    assert utf16_units(text) <= 4096
    assert text.endswith(link)
    assert "\ufffd" not in text
    assert utf16_units(fit_message(emoji * 3000)) <= 4096
    assert fit_message("body", "x" * 4096) == "x" * 4096
    with pytest.raises(RssError) as error:
        fit_message("", "x" * 4097)
    assert error.value.code == "rss_link_too_long"


def test_keyword_include_any_exclude_wins_and_summary_limit() -> None:
    entry = SimpleNamespace(
        title="Python",
        summary="Feeds " * 400,
        link="https://example.com/a",
        feed_title="RSS",
        published_at=None,
    )
    assert matches_keywords(payload(include_keywords=["python", "absent"]), entry)
    assert not matches_keywords(payload(include_keywords=["absent"]), entry)
    assert not matches_keywords(payload(exclude_keywords=["FEEDS"]), entry)
    text = render_entry(payload(message_template="{summary}", max_summary_length=100), entry)
    assert text.split("\n\n")[0] == entry.summary[:100].strip()


def test_rss_settings_private_hosts_and_secrets(monkeypatch) -> None:
    monkeypatch.setenv(
        "RSS_ALLOWED_PRIVATE_HOSTS", "rsshub.internal,127.0.0.1,[::1],RSSHUB.INTERNAL."
    )
    settings = Settings(_env_file=None, rss_bot_token=" 123:secret ")
    assert settings.rss_allowed_private_hosts == ["rsshub.internal", "127.0.0.1", "::1"]
    assert settings.rss_bot_token == "123:secret"
    assert "123:secret" not in repr(settings)
    assert "rss_bot_token" not in settings.model_dump()
    assert settings.rss_enabled and settings.rss_max_feed_bytes == 2 * 1024 * 1024
    assert Settings(_env_file=None, rss_bot_token=" ").rss_bot_token is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"rss_poll_seconds": 0},
        {"rss_request_timeout_seconds": 0},
        {"rss_request_timeout_seconds": float("inf")},
        {"rss_send_interval_seconds": 0},
        {"rss_send_interval_seconds": float("nan")},
        {"rss_max_feed_bytes": 0},
        {"rss_max_entries_per_feed": 0},
        {"rss_max_send_attempts": 0},
        {"rss_allowed_private_hosts": "https://rsshub.internal"},
        {"rss_allowed_private_hosts": "*.internal"},
    ],
)
def test_invalid_rss_settings(overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def _keyword_entry() -> SimpleNamespace:
    return SimpleNamespace(
        title="免费领取加速器",
        summary="今天有好消息，免费开放注册",
        link="https://example.com/1",
        feed_title="feed",
        published_at=None,
        translated_title=None,
        translated_summary=None,
    )


def test_keywords_line_at_bottom_and_hidden_when_not_matched() -> None:
    entry = _keyword_entry()
    lines = render_entry(
        payload(include_keywords=["免费", "好消息", "白嫖"]), entry
    ).splitlines()
    # 命中词按订阅里的填写顺序展示，位置在链接上方。
    assert lines[-2:] == ["🔑 关键词：免费、好消息", "🔗 https://example.com/1"]
    # 一条都没命中、以及没设关键词（不过滤）时都不留孤立图标。
    assert "🔑" not in render_entry(payload(include_keywords=["白嫖"]), entry)
    assert "🔑" not in render_entry(payload(), entry)


def test_keywords_line_falls_back_for_legacy_template() -> None:
    entry = _keyword_entry()
    legacy = "📡 {subscription_name}\n\n# {title}\n\n{summary}\n\n🔗 {link}"
    text = render_entry(
        payload(message_template=legacy, include_keywords=["免费"]), entry
    )
    assert text.splitlines()[-2:] == ["🔑 关键词：免费", "🔗 https://example.com/1"]


def test_keywords_placeholder_allows_custom_position() -> None:
    entry = _keyword_entry()
    template = "{title}\n🔑 {keywords}\n\n{summary}\n\n{link}"
    lines = render_entry(
        payload(message_template=template, include_keywords=["免费", "好消息"]), entry
    ).splitlines()
    assert lines[:2] == ["免费领取加速器", "🔑 免费、好消息"]
    # 自定义位置时不再重复补一行。
    assert sum("🔑" in line for line in lines) == 1
