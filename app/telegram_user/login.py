from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient

from app.config import get_settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


async def login() -> None:
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level)
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH before logging in")

    client = TelegramClient(
        str(settings.telegram_session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    async with client:
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info("Telegram session is already authorized as %s (%s)", me.username, me.id)
            return
        await client.start(phone=settings.telegram_phone)
        me = await client.get_me()
        logger.info("Telegram session authorized as %s (%s)", me.username, me.id)


def cli() -> None:
    asyncio.run(login())


if __name__ == "__main__":
    cli()
