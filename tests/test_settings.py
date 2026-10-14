from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_RSS_TRANSLATE_API_URL, Settings


def test_admin_user_ids_parse_csv() -> None:
    settings = Settings(admin_telegram_user_ids="1, 2,3")
    assert settings.admin_telegram_user_ids == [1, 2, 3]


def test_admin_user_ids_parse_list() -> None:
    settings = Settings(admin_telegram_user_ids=["1", 2, "3"])
    assert settings.admin_telegram_user_ids == [1, 2, 3]


def test_link_preview_media_forwarding_defaults_to_false() -> None:
    assert Settings().telegram_forward_link_preview_media is False


def test_link_preview_media_forwarding_parses_true() -> None:
    settings = Settings(telegram_forward_link_preview_media="true")
    assert settings.telegram_forward_link_preview_media is True


def test_mini_app_allowed_origins_parse_csv() -> None:
    settings = Settings(mini_app_allowed_origins="https://a.example, https://b.example")
    assert settings.mini_app_allowed_origins == ["https://a.example", "https://b.example"]


def test_mini_app_enabled_requires_admin_bot_token_and_admin_ids() -> None:
    settings = Settings(mini_app_enabled=True)
    try:
        settings.validate_runtime_requirements()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("validate_runtime_requirements should fail")

    assert "TG_ADMIN_BOT_TOKEN" in message
    assert "ADMIN_TELEGRAM_USER_IDS" in message


def test_legacy_telegram_session_becomes_default_account() -> None:
    settings = Settings(
        telegram_session_path=Path("data/sessions/user.session"),
        telegram_phone="+8613800000000",
    )
    assert len(settings.telegram_accounts) == 1
    account = settings.telegram_accounts[0]
    assert account.id == "default"
    assert account.session_path == Path("data/sessions/user.session")
    assert account.phone == "+8613800000000"


def test_telegram_accounts_json_default_session_uses_account_subdir() -> None:
    settings = Settings(
        telegram_sessions_dir=Path("data/sessions"),
        telegram_accounts_json='[{"id":"main","phone":"+86111"},{"id":"news"}]',
    )
    assert [item.id for item in settings.telegram_accounts] == ["main", "news"]
    assert settings.get_telegram_account("main").session_path == Path(
        "data/sessions/main/account.session"
    )
    assert settings.get_telegram_account("news").session_path == Path(
        "data/sessions/news/account.session"
    )


def test_telegram_accounts_json_keeps_explicit_session_paths() -> None:
    settings = Settings(
        telegram_accounts_json=(
            '[{"id":"main","session_path":"data/sessions/main.session","phone":"+86111"},'
            '{"id":"news","session_path":"data/sessions/news/account.session"}]'
        )
    )
    assert settings.get_telegram_account("main").session_path == Path("data/sessions/main.session")
    assert settings.get_telegram_account("news").session_path == Path(
        "data/sessions/news/account.session"
    )


def test_telegram_bot_proxy_parsing() -> None:
    assert Settings(telegram_bot_proxy=None).telegram_bot_proxy is None
    assert Settings(telegram_bot_proxy="   ").telegram_bot_proxy is None
    assert (
        Settings(telegram_bot_proxy="http://127.0.0.1:7890").telegram_bot_proxy
        == "http://127.0.0.1:7890"
    )
    assert (
        Settings(telegram_bot_proxy="socks5://user:pass@10.0.0.2:1080").telegram_bot_proxy
        == "socks5://user:pass@10.0.0.2:1080"
    )
    try:
        Settings(telegram_bot_proxy="127.0.0.1:7890")
    except ValueError as exc:
        assert "TELEGRAM_BOT_PROXY" in str(exc)
    else:
        raise AssertionError("proxy without scheme should be rejected")


def test_telegram_accounts_json_directory_session_path_appends_account_file() -> None:
    settings = Settings(
        telegram_accounts_json='[{"id":"work","session_path":"data/sessions/work"}]',
    )
    assert settings.get_telegram_account("work").session_path == Path(
        "data/sessions/work/account.session"
    )


def test_rss_translate_api_url_default_and_empty_falls_back() -> None:
    assert Settings(_env_file=None).rss_translate_api_url == DEFAULT_RSS_TRANSLATE_API_URL
    assert (
        Settings(_env_file=None, rss_translate_api_url="").rss_translate_api_url
        == DEFAULT_RSS_TRANSLATE_API_URL
    )
    assert (
        Settings(_env_file=None, rss_translate_api_url="   ").rss_translate_api_url
        == DEFAULT_RSS_TRANSLATE_API_URL
    )
    custom = "http://127.0.0.1:11434/v1/chat/completions"
    assert Settings(_env_file=None, rss_translate_api_url=custom).rss_translate_api_url == custom
    # 接口地址是普通字段，会进入序列化；凭据是 API Key。
    assert (
        Settings(_env_file=None).model_dump()["rss_translate_api_url"]
        == DEFAULT_RSS_TRANSLATE_API_URL
    )


@pytest.mark.parametrize(
    "value",
    [
        "ftp://api.example.com/v1/chat/completions",
        "api.openai.com/v1/chat/completions",
        "https://",
        "https:///translate",
        "not a url",
        "x" * 3000,
    ],
)
def test_rss_translate_api_url_rejects_invalid(value) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rss_translate_api_url=value)


def test_rss_translate_api_key_is_treated_as_credential() -> None:
    settings = Settings(_env_file=None, rss_translate_api_key="sk-secret-value")
    assert settings.rss_translate_api_key == "sk-secret-value"
    assert "sk-secret-value" not in repr(settings)
    assert "rss_translate_api_key" not in settings.model_dump()


def test_rss_translate_target_default_and_validation() -> None:
    assert Settings(_env_file=None).rss_translate_target == "ZH"
    assert Settings(_env_file=None, rss_translate_target=" en ").rss_translate_target == "en"
    assert (
        Settings(_env_file=None, rss_translate_target="zh-Hant").rss_translate_target == "zh-Hant"
    )
    for bad in ["zh_Hans", "", "zh-Hans-CN", "1", "zhhans!"]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, rss_translate_target=bad)

