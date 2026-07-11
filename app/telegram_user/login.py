from __future__ import annotations

import argparse
import asyncio
import logging

from telethon import TelegramClient

from app.config import Settings, get_settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


async def login_account(settings: Settings, account_id: str) -> None:
    account = settings.get_telegram_account(account_id)
    if account is None:
        known = ", ".join(item.id for item in settings.telegram_accounts) or "(none)"
        raise RuntimeError(f"Unknown Telegram account id: {account_id}. Known: {known}")
    if not account.enabled:
        logger.warning("Telegram account %s is disabled; login will still proceed", account.id)

    api_id = account.resolved_api_id(settings.telegram_api_id)
    api_hash = account.resolved_api_hash(settings.telegram_api_hash)
    if not api_id or not api_hash:
        raise RuntimeError(
            f"Set TELEGRAM_API_ID/TELEGRAM_API_HASH (or per-account api_id/api_hash) "
            f"before logging in account {account.id}"
        )

    account.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(account.session_path), api_id, api_hash)
    async with client:
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(
                "Telegram session already authorized account=%s as %s (%s)",
                account.id,
                getattr(me, "username", None),
                getattr(me, "id", None),
            )
            return
        await client.start(phone=account.phone)
        me = await client.get_me()
        logger.info(
            "Telegram session authorized account=%s as %s (%s)",
            account.id,
            getattr(me, "username", None),
            getattr(me, "id", None),
        )


async def login(account: str | None = None) -> None:
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level)
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        # Per-account credentials may still work, but keep the common case explicit.
        if not any(
            account_cfg.api_id and account_cfg.api_hash
            for account_cfg in settings.telegram_accounts
        ):
            raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH before logging in")

    if account in (None, "", "all"):
        targets = [item.id for item in settings.telegram_accounts]
    else:
        targets = [account]

    if not targets:
        raise RuntimeError("No Telegram accounts configured")

    for account_id in targets:
        await login_account(settings, account_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authorize Telegram user account session(s)")
    parser.add_argument(
        "--account",
        default=None,
        help="Account id to login, or 'all' for every configured account (default: all)",
    )
    return parser


def cli(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    asyncio.run(login(account=args.account))


if __name__ == "__main__":
    cli()
