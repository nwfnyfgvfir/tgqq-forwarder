from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.config import Settings, TelegramAccountConfig
from app.rules.models import TelegramForwardMessage
from app.telegram_user.client import MessageCallback, TelegramUserListener
from app.telegram_user.dialog_cache import TelegramDialogInfo

logger = logging.getLogger(__name__)

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(slots=True)
class TelegramAccountStatus:
    id: str
    enabled: bool
    connected: bool
    authorized: bool
    user_id: int | None
    username: str | None
    phone: str | None
    session_path: str
    last_error: str | None = None


class TelegramAccountManager:
    """Owns one Telethon listener per configured Telegram user account."""

    def __init__(
        self,
        settings: Settings,
        on_message: MessageCallback,
        *,
        reconnect_enabled: bool | None = None,
        reconnect_delay_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.on_message = on_message
        self.reconnect_enabled = (
            settings.telegram_reconnect_enabled if reconnect_enabled is None else reconnect_enabled
        )
        self.reconnect_delay_seconds = (
            settings.telegram_reconnect_delay_seconds
            if reconnect_delay_seconds is None
            else reconnect_delay_seconds
        )
        self._listeners: dict[str, TelegramUserListener] = {}
        self._watch_tasks: dict[str, asyncio.Task] = {}
        self._last_errors: dict[str, str | None] = {}
        self._stopping = asyncio.Event()
        self._started = False

    @property
    def account_ids(self) -> list[str]:
        return [account.id for account in self.settings.telegram_accounts if account.enabled]

    @property
    def listeners(self) -> list[TelegramUserListener]:
        return list(self._listeners.values())

    def get(self, account_id: str | None = None) -> TelegramUserListener | None:
        if account_id:
            return self._listeners.get(account_id)
        if len(self._listeners) == 1:
            return next(iter(self._listeners.values()))
        for candidate in ("default", "main"):
            if candidate in self._listeners:
                return self._listeners[candidate]
        return next(iter(self._listeners.values()), None)

    def is_any_connected(self) -> bool:
        return any(listener.is_connected for listener in self._listeners.values())

    def list_status(self) -> list[TelegramAccountStatus]:
        statuses: list[TelegramAccountStatus] = []
        for account in self.settings.telegram_accounts:
            listener = self._listeners.get(account.id)
            statuses.append(
                TelegramAccountStatus(
                    id=account.id,
                    enabled=account.enabled,
                    connected=bool(listener and listener.is_connected),
                    authorized=bool(listener and listener.is_authorized),
                    user_id=listener.account_user_id if listener else None,
                    username=listener.account_username if listener else None,
                    phone=account.phone,
                    session_path=str(account.session_path),
                    last_error=self._last_errors.get(account.id),
                )
            )
        return statuses

    async def start(self) -> None:
        if self._started:
            return
        self._stopping.clear()
        accounts = [account for account in self.settings.telegram_accounts if account.enabled]
        if not accounts:
            raise RuntimeError("No enabled Telegram accounts configured")

        errors: list[str] = []
        for account in accounts:
            try:
                await self._start_account(account, start_watch=True)
            except Exception as exc:
                self._last_errors[account.id] = str(exc)
                errors.append(f"{account.id}: {exc}")
                logger.exception("Failed to start Telegram account %s", account.id)
                if self.settings.telegram_require_all_accounts:
                    await self.stop()
                    raise RuntimeError(
                        "Failed to start required Telegram accounts: " + "; ".join(errors)
                    ) from exc

        if not self._listeners:
            raise RuntimeError(
                "No Telegram accounts started successfully: " + ("; ".join(errors) or "unknown")
            )
        if errors:
            logger.warning(
                "Started with partial Telegram accounts. Failures: %s",
                "; ".join(errors),
            )
        self._started = True
        logger.info(
            "Telegram account manager started with %s account(s): %s",
            len(self._listeners),
            ", ".join(sorted(self._listeners)),
        )

    async def stop(self) -> None:
        self._stopping.set()
        watch_tasks = list(self._watch_tasks.values())
        for task in watch_tasks:
            task.cancel()
        for task in watch_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._watch_tasks.clear()

        for account_id, listener in list(self._listeners.items()):
            try:
                await listener.stop()
            except Exception:
                logger.exception("Failed to stop Telegram account %s", account_id)
        self._listeners.clear()
        self._started = False
        logger.info("Telegram account manager stopped")

    async def wait_any_disconnected(self) -> None:
        """Block until any started account disconnects while reconnect is disabled."""
        while not self._stopping.is_set():
            if not self._listeners:
                await asyncio.sleep(0.2)
                continue
            wait_tasks = [
                asyncio.create_task(listener.wait_disconnected(), name=f"tg-disc-{account_id}")
                for account_id, listener in self._listeners.items()
            ]
            done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                try:
                    task.result()
                except Exception:
                    logger.debug("disconnect wait task failed", exc_info=True)
            if self._stopping.is_set():
                return
            if not self.reconnect_enabled:
                return
            await asyncio.sleep(self.reconnect_delay_seconds)

    async def list_dialogs(
        self,
        *,
        account_id: str | None = None,
        limit: int = 50,
        query: str | None = None,
    ) -> list[TelegramDialogInfo]:
        listener = self._resolve_listener(account_id)
        if listener is None:
            return []
        return await listener.dialogs.list_dialogs(limit=limit, query=query)

    def _resolve_listener(self, account_id: str | None) -> TelegramUserListener | None:
        if account_id:
            listener = self._listeners.get(account_id)
            if listener is None:
                raise KeyError(f"Telegram account not found or not started: {account_id}")
            return listener
        return self.get()

    async def _start_account(
        self,
        account: TelegramAccountConfig,
        *,
        start_watch: bool,
    ) -> None:
        validate_account_id(account.id)
        listener = TelegramUserListener(
            self.settings,
            self._wrap_on_message(account.id),
            account=account,
        )
        await listener.start()
        self._listeners[account.id] = listener
        self._last_errors[account.id] = None
        if start_watch and self.reconnect_enabled and account.id not in self._watch_tasks:
            self._watch_tasks[account.id] = asyncio.create_task(
                self._watch_account(account.id),
                name=f"tg-watch-{account.id}",
            )

    def _wrap_on_message(self, account_id: str) -> MessageCallback:
        async def _callback(message: TelegramForwardMessage) -> None:
            if not message.account_id:
                message.account_id = account_id
            await self.on_message(message)

        return _callback

    async def _watch_account(self, account_id: str) -> None:
        try:
            while not self._stopping.is_set():
                listener = self._listeners.get(account_id)
                if listener is None:
                    account = self.settings.get_telegram_account(account_id)
                    if account is None or not account.enabled:
                        return
                    try:
                        await self._start_account(account, start_watch=False)
                        logger.info("Telegram account %s reconnected", account_id)
                        continue
                    except Exception as exc:
                        self._last_errors[account_id] = str(exc)
                        logger.exception("Failed to reconnect Telegram account %s", account_id)
                        await asyncio.sleep(self.reconnect_delay_seconds)
                        continue

                try:
                    await listener.wait_disconnected()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Telegram account %s disconnect wait failed", account_id)

                if self._stopping.is_set() or not self.reconnect_enabled:
                    return

                logger.warning(
                    "Telegram account %s disconnected; reconnecting in %.1fs",
                    account_id,
                    self.reconnect_delay_seconds,
                )
                old = self._listeners.pop(account_id, None)
                if old is not None:
                    try:
                        await old.stop()
                    except Exception:
                        logger.exception(
                            "Failed to cleanup disconnected Telegram account %s",
                            account_id,
                        )
                await asyncio.sleep(self.reconnect_delay_seconds)
        finally:
            self._watch_tasks.pop(account_id, None)


def validate_account_id(account_id: str) -> str:
    value = account_id.strip()
    if not _ACCOUNT_ID_RE.fullmatch(value):
        raise ValueError(
            "Telegram account id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}, "
            f"got: {account_id!r}"
        )
    return value


def resolve_account_ids(
    accounts: Sequence[TelegramAccountConfig],
    selected: str | None,
) -> list[str]:
    if selected in (None, "", "all"):
        return [account.id for account in accounts]
    account_id = validate_account_id(selected)
    ids = {account.id for account in accounts}
    if account_id not in ids:
        raise KeyError(f"Unknown Telegram account id: {account_id}")
    return [account_id]
