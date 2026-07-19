from __future__ import annotations

import asyncio
import logging

import uvicorn

from app.config import Settings
from app.rules.service import ForwardRuleService
from app.web.app import create_mini_app

logger = logging.getLogger(__name__)


class _EmbeddedUvicornServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        return None


class MiniAppServer:
    def __init__(
        self,
        settings: Settings,
        service: ForwardRuleService,
        account_manager_getter,
        qq_status_getter,
        qq_targets_getter,
        queue_status_getter,
    ) -> None:
        self.settings = settings
        self.app = create_mini_app(
            settings=settings,
            service=service,
            account_manager_getter=account_manager_getter,
            qq_status_getter=qq_status_getter,
            qq_targets_getter=qq_targets_getter,
            queue_status_getter=queue_status_getter,
        )
        self.server: _EmbeddedUvicornServer | None = None
        self._task: asyncio.Task | None = None

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    async def start(self) -> None:
        if not self.settings.mini_app_enabled:
            logger.info("Telegram Mini App server disabled")
            return
        if self._task is not None:
            return
        config = uvicorn.Config(
            self.app,
            host=self.settings.mini_app_host,
            port=self.settings.mini_app_port,
            log_config=None,
            access_log=False,
        )
        self.server = _EmbeddedUvicornServer(config)
        self._task = asyncio.create_task(self.server.serve(), name="telegram-mini-app-server")
        await asyncio.sleep(0)
        logger.info(
            "Telegram Mini App server started on %s:%s",
            self.settings.mini_app_host,
            self.settings.mini_app_port,
        )

    async def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self.server = None
        logger.info("Telegram Mini App server stopped")
