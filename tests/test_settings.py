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
