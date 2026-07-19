from __future__ import annotations

import logging
from collections.abc import Callable

from telegram import BotCommand, MenuButtonWebApp, WebAppInfo
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import Settings
from app.qq_official.client import QQTargetInfo
from app.rules.service import ForwardRuleService
from app.telegram_admin.auth import AdminAuth
from app.telegram_admin.commands import AdminCommands
from app.telegram_user.accounts import TelegramAccountManager

logger = logging.getLogger(__name__)


class TelegramAdminBot:
    def __init__(
        self,
        settings: Settings,
        service: ForwardRuleService,
        account_manager_getter: Callable[[], TelegramAccountManager | None],
        qq_status_getter: Callable[[], str],
        qq_targets_getter: Callable[[], list[QQTargetInfo]],
        queue_status_getter: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.settings = settings
        self.service = service
        self.account_manager_getter = account_manager_getter
        self.qq_status_getter = qq_status_getter
        self.qq_targets_getter = qq_targets_getter
        self.queue_status_getter = queue_status_getter or (lambda: {})
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
            self.account_manager_getter,
            self.qq_status_getter,
            self.qq_targets_getter,
            self.queue_status_getter,
        )
        app = (
            Application.builder()
            .token(self.settings.tg_admin_bot_token)
            .connect_timeout(self.settings.tg_admin_bot_connect_timeout)
            .read_timeout(self.settings.tg_admin_bot_request_timeout)
            .write_timeout(self.settings.tg_admin_bot_request_timeout)
            .pool_timeout(self.settings.tg_admin_bot_pool_timeout)
            .get_updates_connect_timeout(self.settings.tg_admin_bot_connect_timeout)
            .get_updates_read_timeout(self.settings.tg_admin_bot_poll_read_timeout)
            .get_updates_write_timeout(self.settings.tg_admin_bot_request_timeout)
            .get_updates_pool_timeout(self.settings.tg_admin_bot_pool_timeout)
            .build()
        )
        app.add_error_handler(self._handle_error)
        app.add_handler(CommandHandler(["start", "help"], commands.start))
        app.add_handler(CommandHandler("status", commands.status))
        app.add_handler(CommandHandler("accounts", commands.accounts))
        app.add_handler(CommandHandler("dialogs", commands.dialogs))
        app.add_handler(CommandHandler("rules", commands.rules))
        app.add_handler(CommandHandler("qq_targets", commands.qq_targets))
        app.add_handler(CommandHandler("add_rule", commands.add_rule))
        app.add_handler(CommandHandler("del_rule", commands.del_rule))
        app.add_handler(CommandHandler("enable_rule", commands.enable_rule))
        app.add_handler(CommandHandler("disable_rule", commands.disable_rule))
        app.add_handler(CommandHandler("logs", commands.logs))
        app.add_handler(CommandHandler("errors", commands.errors))
        app.add_handler(CommandHandler("pause", commands.pause))
        app.add_handler(CommandHandler("resume", commands.resume))
        app.add_handler(CommandHandler("app", commands.mini_app))

        await app.initialize()
        await self._set_bot_commands(app)
        await app.start()
        await app.updater.start_polling(timeout=self.settings.tg_admin_bot_poll_timeout)
        self.application = app
        logger.info("Telegram admin bot started")

    async def _handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(context.error, (NetworkError, TimedOut)):
            logger.warning("Telegram admin bot network error", exc_info=context.error)
            return
        logger.exception("Telegram admin bot handler error", exc_info=context.error)

    async def _set_bot_commands(self, app: Application) -> None:
        try:
            await app.bot.set_my_commands(
                [
                    BotCommand("start", "显示帮助信息"),
                    BotCommand("status", "查看运行状态"),
                    BotCommand("accounts", "查看 Telegram 账号状态"),
                    BotCommand("dialogs", "查看或搜索 Telegram 会话"),
                    BotCommand("rules", "查看转发规则"),
                    BotCommand("qq_targets", "查看已缓存的 QQ 目标 ID"),
                    BotCommand("add_rule", "新增转发规则"),
                    BotCommand("del_rule", "删除转发规则"),
                    BotCommand("enable_rule", "启用转发规则"),
                    BotCommand("disable_rule", "禁用转发规则"),
                    BotCommand("logs", "查看最近转发日志"),
                    BotCommand("errors", "查看最近错误日志"),
                    BotCommand("pause", "暂停全部转发"),
                    BotCommand("resume", "恢复全部转发"),
                    BotCommand("app", "打开 Mini App 管理台"),
                ]
            )
            if self.settings.mini_app_public_url:
                await app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="TGQQ 管理台",
                        web_app=WebAppInfo(url=self.settings.mini_app_public_url),
                    )
                )
        except (NetworkError, TelegramError, TimedOut):
            logger.warning("Failed to register Telegram admin bot commands", exc_info=True)
            return
        logger.info("Telegram admin bot commands have been registered")

    async def stop(self) -> None:
        if self.application is None:
            return
        if self.application.updater:
            await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()
        logger.info("Telegram admin bot stopped")
