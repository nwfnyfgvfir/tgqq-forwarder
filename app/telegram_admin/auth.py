from __future__ import annotations

from telegram import Update

from app.config import Settings


class AdminAuth:
    def __init__(self, settings: Settings) -> None:
        self.admin_ids = set(settings.admin_telegram_user_ids)

    def is_allowed(self, update: Update) -> bool:
        if not self.admin_ids:
            return False
        user = update.effective_user
        return bool(user and user.id in self.admin_ids)
