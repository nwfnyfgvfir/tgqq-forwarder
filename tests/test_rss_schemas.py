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
    assert text.splitlines()[2] == format_published_at(entry.published_at)
    assert "\n\n\n" not in text

    without_time = SimpleNamespace(**{**entry.__dict__, "published_at": None})
    blank = render_entry(payload(), without_time)
    assert blank.splitlines()[0] == "[rss]"
    assert blank.splitlines()[1] == "每周分享"
    assert "\n\n\n" not in blank
    assert format_published_at(None) == ""


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
