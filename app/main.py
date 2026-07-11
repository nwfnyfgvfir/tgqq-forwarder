from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from app.config import get_settings
from app.logging_config import configure_logging
from app.qq_official.sender import QQOfficialSender
from app.rules.service import ForwardRuleService
from app.storage.db import Database
from app.telegram_admin.bot import TelegramAdminBot
from app.telegram_user.accounts import TelegramAccountManager
from app.web.server import MiniAppServer
from app.worker.forward_queue import ForwardQueue
from app.worker.media_cleanup import MediaCleanupWorker

logger = logging.getLogger(__name__)


class ApplicationRuntime:
    def __init__(self) -> None:
        self.settings = get_settings()
        configure_logging(self.settings.log_dir, self.settings.log_level)
        self.db = Database(self.settings.database_url)
        self.rule_service = ForwardRuleService(self.db)
        self.qq_sender = QQOfficialSender(self.settings)
        self.forward_queue = ForwardQueue(self.settings, self.rule_service, self.qq_sender)
        self.media_cleanup = MediaCleanupWorker(self.settings)
        self.account_manager: TelegramAccountManager | None = None
        self.admin_bot = TelegramAdminBot(
            self.settings,
            self.rule_service,
            lambda: self.account_manager,
            lambda: self.qq_sender.status,
            self.qq_sender.list_cached_targets,
        )
        self.mini_app_server = MiniAppServer(
            self.settings,
            self.rule_service,
            lambda: self.account_manager,
            lambda: self.qq_sender.status,
            self.qq_sender.list_cached_targets,
            lambda: self.forward_queue.size,
        )
        self.stop_event = asyncio.Event()

    async def start(self) -> None:
        self.settings.validate_runtime_requirements()
        await self.db.init()
        await self.qq_sender.start()
        await self.forward_queue.start()
        await self.media_cleanup.start()
        self.account_manager = TelegramAccountManager(
            self.settings,
            self.forward_queue.enqueue,
        )
        await self.account_manager.start()
        await self.admin_bot.start()
        await self.mini_app_server.start()
        logger.info("TGQQ Forwarder started")

    async def run_until_stopped(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop_event.set)

        wait_tasks = [asyncio.create_task(self.stop_event.wait(), name="stop-event")]
        if self.account_manager and not self.settings.telegram_reconnect_enabled:
            wait_tasks.append(
                asyncio.create_task(
                    self.account_manager.wait_any_disconnected(),
                    name="telegram-disconnected",
                )
            )
        if self.mini_app_server.task:
            wait_tasks.append(self.mini_app_server.task)
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with suppress(asyncio.CancelledError):
                task.result()

    async def stop(self) -> None:
        logger.info("Stopping TGQQ Forwarder")
        await self.mini_app_server.stop()
        await self.admin_bot.stop()
        if self.account_manager:
            await self.account_manager.stop()
        await self.media_cleanup.stop()
        await self.forward_queue.stop()
        await self.qq_sender.stop()
        await self.db.dispose()


async def amain() -> None:
    runtime = ApplicationRuntime()
    try:
        await runtime.start()
        await runtime.run_until_stopped()
    finally:
        await runtime.stop()


def cli() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    cli()
