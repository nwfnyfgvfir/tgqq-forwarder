from __future__ import annotations

from app.config import Settings


def test_admin_user_ids_parse_csv() -> None:
    settings = Settings(admin_telegram_user_ids="1, 2,3")
    assert settings.admin_telegram_user_ids == [1, 2, 3]


def test_admin_user_ids_parse_list() -> None:
    settings = Settings(admin_telegram_user_ids=["1", 2, "3"])
    assert settings.admin_telegram_user_ids == [1, 2, 3]
