from __future__ import annotations

import logging
from collections.abc import Callable

from telegram import BotCommand
from telegram.ext import Application, CommandHandler

from app.config import Settings
from app.rules.service import ForwardRuleService
from app.telegram_admin.auth import AdminAuth
from app.telegram_admin.commands import AdminCommands
from app.telegram_user.client import TelegramUserListener

logger = logging.getLogger(__name__)


class TelegramAdminBot:
    def __init__(
        self,
        settings: Settings,
        service: ForwardRuleService,
        telegram_listener_getter: Callable[[], TelegramUserListener | None],
        qq_status_getter: Callable[[], str],
    ) -> None:
        self.settings = settings
        self.service = service
        self.telegram_listener_getter = telegram_listener_getter
        self.qq_status_getter = qq_status_getter
        self.application: Application | None = None

    async def start(self) -> None:
        if not self.settings.tg_admin_bot_token:
            logger.warning("TG_ADMIN_BOT_TOKEN is not set; admin bot disabled")
            return
        if not self.settings.admin_telegram_user_ids:
            logger.warning("ADMIN_TELEGRAM_USER_IDS is empty; admin bot disabled")
            return

        auth = AdminAuth(self.settings)
        commands = AdminCommands(
            self.settings,
            self.service,
            auth,
            self.telegram_listener_getter,
            self.qq_status_getter,
        )
        app = Application.builder().token(self.settings.tg_admin_bot_token).build()
        app.add_handler(CommandHandler(["start", "help"], commands.start))
        app.add_handler(CommandHandler("status", commands.status))
        app.add_handler(CommandHandler("dialogs", commands.dialogs))
        app.add_handler(CommandHandler("rules", commands.rules))
        app.add_handler(CommandHandler("add_rule", commands.add_rule))
        app.add_handler(CommandHandler("del_rule", commands.del_rule))
        app.add_handler(CommandHandler("enable_rule", commands.enable_rule))
        app.add_handler(CommandHandler("disable_rule", commands.disable_rule))
        app.add_handler(CommandHandler("logs", commands.logs))
        app.add_handler(CommandHandler("errors", commands.errors))
        app.add_handler(CommandHandler("pause", commands.pause))
        app.add_handler(CommandHandler("resume", commands.resume))

        await app.initialize()
        await self._set_bot_commands(app)
        await app.start()
        await app.updater.start_polling()
        self.application = app
        logger.info("Telegram admin bot started")

    async def _set_bot_commands(self, app: Application) -> None:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "显示帮助信息"),
                BotCommand("status", "查看运行状态"),
                BotCommand("dialogs", "查看或搜索 Telegram 会话"),
                BotCommand("rules", "查看转发规则"),
                BotCommand("add_rule", "新增转发规则"),
                BotCommand("del_rule", "删除转发规则"),
                BotCommand("enable_rule", "启用转发规则"),
                BotCommand("disable_rule", "禁用转发规则"),
                BotCommand("logs", "查看最近转发日志"),
                BotCommand("errors", "查看最近错误日志"),
                BotCommand("pause", "暂停全部转发"),
                BotCommand("resume", "恢复全部转发"),
            ]
        )
        logger.info("Telegram admin bot commands have been registered")

    async def stop(self) -> None:
        if self.application is None:
            return
        if self.application.updater:
            await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()
        logger.info("Telegram admin bot stopped")
