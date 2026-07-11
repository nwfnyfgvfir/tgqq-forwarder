from __future__ import annotations

import html
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import ContextTypes

from app.config import Settings
from app.qq_official.client import QQTargetInfo
from app.rules.keywords import (
    keywords_from_text_include_regex,
    keywords_to_text_include_regex,
    split_keyword_args,
)
from app.rules.service import ForwardRuleService
from app.storage.models import ForwardRule, QQTargetType
from app.telegram_admin.auth import AdminAuth
from app.telegram_user.accounts import TelegramAccountManager

logger = logging.getLogger(__name__)


async def _reply_text(message: Message | None, text: str, **kwargs: object) -> None:
    if message is None:
        return
    try:
        await message.reply_text(text, **kwargs)
    except (NetworkError, TelegramError, TimedOut):
        logger.warning("Failed to send Telegram admin bot reply", exc_info=True)


def _escape(value: object) -> str:
    return html.escape(str(value))


@dataclass(slots=True)
class _ParsedAddRuleArgs:
    name: str
    source_account_id: str | None
    source_chat_id: int | None
    source_sender_id: int | None
    target_type: str
    target_id: str
    keywords: list[str]


def _parse_int_or_wildcard(raw: str) -> int | None:
    if raw == "*":
        return None
    return int(raw)


def _parse_account_or_wildcard(raw: str) -> str | None:
    if raw == "*":
        return None
    return raw


def parse_add_rule_args(
    args: Sequence[str],
    *,
    known_account_ids: set[str] | None = None,
) -> _ParsedAddRuleArgs | None:
    """Parse /add_rule args.

    Supported shapes:
    1) <name...> <chat|*> <sender|*> <qq_type> <qq_id> [keywords...]
    2) <name...> <account|*> <chat|*> <sender|*> <qq_type> <qq_id> [keywords...]

    Account-aware form is only accepted when the account token is ``*`` or a known
    account id, so rule names with spaces are not misparsed as account ids.
    """
    target_types = {item.value for item in QQTargetType}
    known = known_account_ids or set()
    account_candidates: list[_ParsedAddRuleArgs] = []
    legacy_candidates: list[_ParsedAddRuleArgs] = []
    for target_type_index, raw_target_type in enumerate(args):
        target_type = raw_target_type.lower()
        if target_type not in target_types:
            continue
        if target_type_index + 1 >= len(args):
            continue

        if target_type_index >= 4:
            account_token = args[target_type_index - 3]
            if account_token == "*" or account_token in known:
                name_parts = list(args[: target_type_index - 3])
                if name_parts:
                    try:
                        source_account_id = _parse_account_or_wildcard(account_token)
                        source_chat_id = _parse_int_or_wildcard(args[target_type_index - 2])
                        source_sender_id = _parse_int_or_wildcard(args[target_type_index - 1])
                    except ValueError:
                        pass
                    else:
                        account_candidates.append(
                            _ParsedAddRuleArgs(
                                name=" ".join(name_parts),
                                source_account_id=source_account_id,
                                source_chat_id=source_chat_id,
                                source_sender_id=source_sender_id,
                                target_type=target_type,
                                target_id=args[target_type_index + 1],
                                keywords=split_keyword_args(args[target_type_index + 2 :]),
                            )
                        )

        if target_type_index >= 3:
            name_parts = list(args[: target_type_index - 2])
            if name_parts:
                try:
                    source_chat_id = _parse_int_or_wildcard(args[target_type_index - 2])
                    source_sender_id = _parse_int_or_wildcard(args[target_type_index - 1])
                except ValueError:
                    pass
                else:
                    legacy_candidates.append(
                        _ParsedAddRuleArgs(
                            name=" ".join(name_parts),
                            source_account_id=None,
                            source_chat_id=source_chat_id,
                            source_sender_id=source_sender_id,
                            target_type=target_type,
                            target_id=args[target_type_index + 1],
                            keywords=split_keyword_args(args[target_type_index + 2 :]),
                        )
                    )
    if account_candidates:
        return account_candidates[-1]
    if legacy_candidates:
        return legacy_candidates[-1]
    return None


class AdminCommands:
    def __init__(
        self,
        settings: Settings,
        service: ForwardRuleService,
        auth: AdminAuth,
        account_manager_getter: Callable[[], TelegramAccountManager | None],
        qq_status_getter: Callable[[], str],
        qq_targets_getter: Callable[[], list[QQTargetInfo]],
    ) -> None:
        self.settings = settings
        self.service = service
        self.auth = auth
        self.account_manager_getter = account_manager_getter
        self.qq_status_getter = qq_status_getter
        self.qq_targets_getter = qq_targets_getter

    async def _deny_if_needed(self, update: Update) -> bool:
        if self.auth.is_allowed(update):
            return False
        await _reply_text(update.effective_message, "无权限。")
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        await _reply_text(
            update.effective_message,
            "TGQQ Forwarder 管理命令：\n"
            "/status - 查看运行状态\n"
            "/accounts - 查看 Telegram 账号状态\n"
            "/dialogs [账号ID] [关键词] - 查看或搜索 Telegram 会话\n"
            "/rules - 查看转发规则\n"
            "/qq_targets - 查看已缓存的 QQ 目标 ID\n"
            "/add_rule <名称> [TG账号ID|*] <TG会话ID|*> <TG发送者ID|*> "
            "<QQ目标类型> <QQ目标ID> [关键词...] - 新增规则；"
            "名称可含空格，重复规则会合并关键词\n"
            "/del_rule <ID> - 删除规则\n"
            "/enable_rule <ID> - 启用规则\n"
            "/disable_rule <ID> - 禁用规则\n"
            "/logs [数量] - 查看最近转发日志\n"
            "/errors [数量] - 查看最近错误日志\n"
            "/pause - 暂停全部转发\n"
            "/resume - 恢复全部转发\n"
            "/app - 打开 Telegram Mini App 管理台",
            reply_markup=self._mini_app_markup(),
        )

    async def mini_app(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        if not self.settings.mini_app_public_url:
            await _reply_text(update.effective_message, "MINI_APP_PUBLIC_URL 尚未配置。")
            return
        await _reply_text(
            update.effective_message,
            "打开 TGQQ Forwarder Mini App 管理台：",
            reply_markup=self._mini_app_markup(),
        )

    def _mini_app_markup(self) -> InlineKeyboardMarkup | None:
        if not self.settings.mini_app_public_url:
            return None
        try:
            button = InlineKeyboardButton(
                "打开 Mini App 管理台",
                web_app=WebAppInfo(url=self.settings.mini_app_public_url),
            )
        except (TypeError, TelegramError):
            button = InlineKeyboardButton(
                "打开 Mini App 管理台",
                url=self.settings.mini_app_public_url,
            )
        return InlineKeyboardMarkup([[button]])

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        manager = self.account_manager_getter()
        statuses = manager.list_status() if manager else []
        if not statuses:
            telegram_status = "未连接"
        else:
            connected = sum(1 for item in statuses if item.connected)
            telegram_status = f"{connected}/{len(statuses)} 已连接"
        paused = await self.service.is_paused()
        total_rules, enabled_rules, total_logs = await self.service.counts()
        account_lines = []
        for item in statuses[:20]:
            state = "在线" if item.connected else "离线"
            account_lines.append(
                f"- {_escape(item.id)} [{state}] "
                f"{_escape(item.username or '-')} ({item.user_id or '-'})"
            )
        accounts_block = "\n".join(account_lines) if account_lines else "- 无账号"
        await _reply_text(
            update.effective_message,
            "运行状态：\n"
            f"Telegram 用户监听：{telegram_status}\n"
            f"{accounts_block}\n"
            f"QQ WebSocket：{self.qq_status_getter()}\n"
            f"是否暂停转发：{'是' if paused else '否'}\n"
            f"跨账号去重：{'开' if self.settings.telegram_dedupe_cross_account else '关'}\n"
            f"规则数量：启用 {enabled_rules} / 总计 {total_rules}\n"
            f"日志数量：{total_logs}",
            parse_mode=ParseMode.HTML,
        )

    async def accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        manager = self.account_manager_getter()
        statuses = manager.list_status() if manager else []
        if not statuses:
            await _reply_text(update.effective_message, "当前没有配置或启动 Telegram 账号。")
            return
        lines = ["Telegram 账号："]
        for item in statuses:
            state = "在线" if item.connected else "离线"
            enabled = "启用" if item.enabled else "禁用"
            error = f" 错误={_escape(item.last_error)}" if item.last_error else ""
            lines.append(
                f"{_escape(item.id)} [{enabled}/{state}] "
                f"user={_escape(item.username or '-')}({item.user_id or '-'}) "
                f"session=<code>{_escape(item.session_path)}</code>{error}"
            )
        await _reply_text(
            update.effective_message,
            "\n".join(lines)[:3900],
            parse_mode=ParseMode.HTML,
        )

    async def dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        manager = self.account_manager_getter()
        if not manager:
            await _reply_text(update.effective_message, "Telegram 用户监听器尚未就绪。")
            return

        account_id: str | None = None
        query_parts: list[str] = []
        args = list(context.args or [])
        known_ids = {item.id for item in self.settings.telegram_accounts}
        if args:
            if args[0] in known_ids:
                account_id = args[0]
                query_parts = args[1:]
            else:
                query_parts = args
        query = " ".join(query_parts) if query_parts else None

        try:
            dialogs = await manager.list_dialogs(account_id=account_id, limit=80, query=query)
        except KeyError as exc:
            await _reply_text(update.effective_message, str(exc))
            return

        if not dialogs:
            await _reply_text(update.effective_message, "没有找到匹配的 Telegram 会话。")
            return

        selected = account_id or (manager.get().account_id if manager.get() else "*")
        lines = [f"账号={_escape(selected)}"]
        lines.extend(
            f"{item.type} | <code>{item.id}</code> | {_escape(item.name)}"
            for item in dialogs[:50]
        )
        await _reply_text(update.effective_message, "\n".join(lines), parse_mode=ParseMode.HTML)

    async def rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        rules = await self.service.list_rules(limit=50)
        if not rules:
            await _reply_text(update.effective_message, "当前没有转发规则。")
            return
        lines = []
        for rule in rules:
            state = "启用" if rule.enabled else "禁用"
            keywords = keywords_from_text_include_regex(rule.text_include_regex)
            keyword_note = f" 关键词={_escape('、'.join(keywords))}" if keywords else ""
            lines.append(
                f"#{rule.id} [{state}] {_escape(rule.name)} | "
                f"TG账号={_escape(rule.source_account_id or '*')} "
                f"TG会话={rule.source_chat_id or '*'} "
                f"TG发送者={rule.source_sender_id or '*'} "
                f"{keyword_note} -> "
                f"{_escape(rule.qq_target_type)}:{_escape(rule.qq_target_id)}"
            )
        await _reply_text(update.effective_message, "\n".join(lines), parse_mode=ParseMode.HTML)

    async def qq_targets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        targets = self.qq_targets_getter()
        if not targets:
            await _reply_text(
                update.effective_message,
                "还没有缓存到 QQ 目标 ID。\n"
                "请先在目标 QQ 群/C2C/频道里给机器人发一条消息或 @ 机器人，"
                "然后再执行 /qq_targets。"
            )
            return
        lines = [
            "已缓存的 QQ 目标 ID：",
            "格式：类型 | 目标ID | 最近消息ID | 说明",
        ]
        for target in targets[:50]:
            lines.append(
                f"{_escape(target.target_type)} | "
                f"<code>{_escape(target.target_id)}</code> | "
                f"{_escape(target.last_message_id or '-')} | "
                f"{_escape(target.display_name or '-')}"
            )
        lines.append(
            "\n添加规则示例：\n"
            "/add_rule qq_to_group main -1001234567890 * group 上面查到的目标ID\n"
            "/add_rule qq_to_group_ai -1001234567890 * group 上面查到的目标ID AI,Python"
        )
        await _reply_text(
            update.effective_message,
            "\n".join(lines)[:3900],
            parse_mode=ParseMode.HTML,
        )

    async def add_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        known_account_ids = {item.id for item in self.settings.telegram_accounts}
        parsed = parse_add_rule_args(context.args, known_account_ids=known_account_ids)
        if parsed is None:
            await _reply_text(
                update.effective_message,
                "用法：/add_rule <名称> [TG账号ID|*] <TG会话ID|*> <TG发送者ID|*> "
                "<QQ目标类型> <QQ目标ID> [关键词...]\n"
                "示例：/add_rule LINUX DO Channel main -1002035446470 * c2c "
                "QQ_OPENID gpt,注册机,公益\n"
                "兼容旧写法：/add_rule LINUX DO Channel -1002035446470 * c2c "
                "QQ_OPENID gpt,注册机,公益\n"
                "名称可以包含空格；关键词为可选项，可用空格、英文逗号或中文逗号分隔。\n"
                "QQ目标类型可选：group、c2c、channel、dms"
            )
            return
        if parsed.source_account_id is not None:
            known = {item.id for item in self.settings.telegram_accounts}
            if parsed.source_account_id not in known:
                await _reply_text(
                    update.effective_message,
                    f"未知 Telegram 账号：{parsed.source_account_id}",
                )
                return
        rule = ForwardRule(
            name=parsed.name,
            source_account_id=parsed.source_account_id,
            source_chat_id=parsed.source_chat_id,
            source_sender_id=parsed.source_sender_id,
            text_include_regex=(
                keywords_to_text_include_regex(parsed.keywords) if parsed.keywords else None
            ),
            qq_target_type=parsed.target_type,
            qq_target_id=parsed.target_id,
            message_template=self.settings.default_message_template,
        )
        result = await self.service.create_or_merge_rule(rule)
        keyword_note = (
            f"关键词：{'、'.join(result.keywords)}" if result.keywords else "未设置关键词"
        )
        duplicate_note = (
            f"，已删除重复规则 {result.removed_duplicate_count} 条"
            if result.removed_duplicate_count
            else ""
        )
        account_note = f"账号={result.rule.source_account_id or '*'}"
        if result.created:
            prefix = f"已创建规则 #{result.rule.id}"
        elif result.updated:
            prefix = f"已合并到已有规则 #{result.rule.id}"
        else:
            prefix = f"规则已存在 #{result.rule.id}"
        await _reply_text(
            update.effective_message,
            f"{prefix}。{account_note}。{keyword_note}{duplicate_note}。",
        )

    async def del_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        rule_id = self._first_int_arg(context)
        if rule_id is None:
            await _reply_text(update.effective_message, "用法：/del_rule <ID>")
            return
        deleted = await self.service.delete_rule(rule_id)
        await _reply_text(update.effective_message, "已删除。" if deleted else "规则不存在。")

    async def enable_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_rule_enabled(update, context, True)

    async def disable_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_rule_enabled(update, context, False)

    async def _set_rule_enabled(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        enabled: bool,
    ) -> None:
        if await self._deny_if_needed(update):
            return
        rule_id = self._first_int_arg(context)
        if rule_id is None:
            await _reply_text(
                update.effective_message,
                "用法：/enable_rule <ID> 或 /disable_rule <ID>"
            )
            return
        changed = await self.service.set_rule_enabled(rule_id, enabled)
        await _reply_text(update.effective_message, "已更新。" if changed else "规则不存在。")

    async def logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        limit = self._first_int_arg(context) or 20
        rows = await self.service.recent_logs(limit=min(limit, 50))
        await self._send_logs(update, rows)

    async def errors(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        limit = self._first_int_arg(context) or 20
        rows = await self.service.recent_logs(limit=min(limit, 50), status="failed")
        await self._send_logs(update, rows)

    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        await self.service.set_paused(True)
        await _reply_text(update.effective_message, "已暂停全部转发。")

    async def resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        await self.service.set_paused(False)
        await _reply_text(update.effective_message, "已恢复全部转发。")

    async def _send_logs(self, update: Update, rows) -> None:
        if not rows:
            await _reply_text(update.effective_message, "没有日志。")
            return
        lines = []
        for row in rows:
            lines.append(
                f"#{row.id} 状态={row.status} 规则={row.rule_id} "
                f"账号={_escape(row.tg_account_id or '-')} "
                f"TG={row.tg_chat_id}/{row.tg_message_id} -> "
                f"{row.qq_target_type}:{_escape(row.qq_target_id)} "
                f"错误={_escape(row.error_message or '')}"
            )
        text = "\n".join(lines)
        await _reply_text(update.effective_message, text[:3900], parse_mode=ParseMode.HTML)

    @staticmethod
    def _first_int_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
        if not context.args:
            return None
        try:
            return int(context.args[0])
        except ValueError:
            return None
