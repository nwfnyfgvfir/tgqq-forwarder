from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import suppress

from app.backup import DataDirectoryLock, FullBackupService
from app.channels.router import DeliveryRouter
from app.config import Settings, clear_settings_cache, get_settings
from app.logging_config import configure_logging
from app.qq_official.sender import QQOfficialSender
from app.rss.service import RssService
from app.rss.worker import RssWorker
from app.rules.service import ForwardRuleService
from app.storage.db import Database
from app.telegram_admin.bot import TelegramAdminBot
from app.telegram_outbound.delivery import TelegramDelivery
from app.telegram_outbound.registry import SentMessageRegistry
from app.telegram_user.accounts import TelegramAccountManager
from app.web.server import MiniAppServer
from app.weixin.service import WeixinService
from app.worker.forward_queue import ForwardQueue
from app.worker.media_cleanup import MediaCleanupWorker

logger = logging.getLogger(__name__)


class ApplicationRuntime:
    def __init__(self) -> None:
        self.settings = get_settings()
        configure_logging(self.settings.log_dir, self.settings.log_level)
        self.db = Database(self.settings.database_url)
        self.data_lock = DataDirectoryLock(self.settings.data_dir)
        self.rule_service = ForwardRuleService(self.db)
        # 自己发出的 TG 消息登记在这里，监听侧据此跳过，避免 TG → TG 回环。
        self.telegram_registry = SentMessageRegistry()
        self.telegram_delivery = TelegramDelivery(
            self.settings,
            lambda: self.account_manager,
            registry=self.telegram_registry,
        )
        self.qq_sender = QQOfficialSender(self.settings, self._enqueue_forward)
        self.weixin_service = WeixinService(self.settings, self._enqueue_forward)
        self.delivery_router = DeliveryRouter(
            self.settings,
            self.qq_sender,
            self.weixin_service,
            self.telegram_delivery,
        )
        self.forward_queue = ForwardQueue(
            self.settings,
            self.rule_service,
            self.qq_sender,
            self.weixin_service,
            router=self.delivery_router,
        )
        self.media_cleanup = MediaCleanupWorker(self.settings)
        self.account_manager: TelegramAccountManager | None = None
        self.rss_service = RssService(self.db, self.settings)
        self.rss_worker = RssWorker(
            self.settings,
            self.rss_service,
            can_publish=self._can_publish_rss,
        )
        self.admin_bot = TelegramAdminBot(
            self.settings,
            self.rule_service,
            lambda: self.account_manager,
            lambda: self.qq_sender.status,
            self.qq_sender.list_cached_targets,
            self.forward_queue.status_snapshot,
        )
        self.mini_app_server = MiniAppServer(
            self.settings,
            self.rule_service,
            lambda: self.account_manager,
            lambda: self.qq_sender.status,
            self.qq_sender.list_cached_targets,
            self.forward_queue.status_snapshot,
            rss_service=self.rss_service,
            rss_worker=self.rss_worker,
            weixin_service=self.weixin_service,
            restart_callback=self.request_restart,
        )
        self.stop_event = asyncio.Event()
        self.restart_requested = False

    async def request_restart(self) -> None:
        self.restart_requested = True
        self.stop_event.set()

    async def _enqueue_forward(self, message) -> None:
        await self.forward_queue.enqueue(message)

    async def _can_publish_rss(self) -> bool:
        # RSS 始终发布到 Telegram 频道，之后由规则转发到 QQ / 微信，因此以监听连接为准。
        if self.account_manager is None:
            return False
        if not self.account_manager.is_any_connected():
            return False
        return not await self.rule_service.is_paused()

    async def start(self) -> None:
        self.settings.validate_runtime_requirements()
        self.data_lock.acquire()
        await self.db.init()
        if self.settings.qq_enabled:
            await self.qq_sender.start()
        else:
            logger.info("QQ delivery disabled (QQ_ENABLED=false)")
        await self.forward_queue.start()
        await self.media_cleanup.start()
        if self.settings.telegram_enabled:
            self.account_manager = TelegramAccountManager(
                self.settings,
                self.forward_queue.enqueue,
                sent_registry=self.telegram_registry,
            )
            await self.account_manager.start()
        else:
            logger.info("Telegram ingestion disabled (TELEGRAM_ENABLED=false)")
        await self.weixin_service.start()
        await self.admin_bot.start()
        await self.rss_worker.start()
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
        await self.rss_worker.stop()
        await self.admin_bot.stop()
        await self.weixin_service.stop()
        await self.telegram_delivery.stop()
        if self.account_manager:
            await self.account_manager.stop()
        await self.media_cleanup.stop()
        await self.forward_queue.stop()
        await self.delivery_router.close()
        if self.settings.qq_enabled:
            await self.qq_sender.stop()
        await self.db.dispose()
        self.data_lock.release()


async def amain() -> None:
    await asyncio.to_thread(FullBackupService(Settings()).recover_interrupted)
    # No open database/session connections exist until the pending restore has completed.
    if await asyncio.to_thread(FullBackupService(get_settings()).apply_pending):
        clear_settings_cache()
    runtime = ApplicationRuntime()
    try:
        await runtime.start()
        await runtime.run_until_stopped()
    finally:
        await runtime.stop()
    if runtime.restart_requested:
        os.execv(sys.executable, [sys.executable, "-m", "app.main"])


def cli() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    cli()
