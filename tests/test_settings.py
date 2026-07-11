from __future__ import annotations

from pathlib import Path

from app.config import Settings


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


def test_telegram_accounts_json_directory_session_path_appends_account_file() -> None:
    settings = Settings(
        telegram_accounts_json='[{"id":"work","session_path":"data/sessions/work"}]',
    )
    assert settings.get_telegram_account("work").session_path == Path(
        "data/sessions/work/account.session"
    )
