from __future__ import annotations

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
