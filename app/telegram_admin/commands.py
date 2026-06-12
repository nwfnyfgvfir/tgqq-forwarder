from __future__ import annotations

import html
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from telegram import Update
from telegram.constants import ParseMode
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
from app.telegram_user.client import TelegramUserListener

logger = logging.getLogger(__name__)


def _escape(value: object) -> str:
    return html.escape(str(value))


@dataclass(slots=True)
class _ParsedAddRuleArgs:
    name: str
    source_chat_id: int | None
    source_sender_id: int | None
    target_type: str
    target_id: str
    keywords: list[str]


def _parse_int_or_wildcard(raw: str) -> int | None:
    if raw == "*":
        return None
    return int(raw)


def parse_add_rule_args(args: Sequence[str]) -> _ParsedAddRuleArgs | None:
    target_types = {item.value for item in QQTargetType}
    candidates: list[_ParsedAddRuleArgs] = []
    for target_type_index, raw_target_type in enumerate(args):
        target_type = raw_target_type.lower()
        if target_type not in target_types:
            continue
        if target_type_index < 3 or target_type_index + 1 >= len(args):
            continue
        name_parts = list(args[: target_type_index - 2])
        if not name_parts:
            continue
        try:
            source_chat_id = _parse_int_or_wildcard(args[target_type_index - 2])
            source_sender_id = _parse_int_or_wildcard(args[target_type_index - 1])
        except ValueError:
            continue
        candidates.append(
            _ParsedAddRuleArgs(
                name=" ".join(name_parts),
                source_chat_id=source_chat_id,
                source_sender_id=source_sender_id,
                target_type=target_type,
                target_id=args[target_type_index + 1],
                keywords=split_keyword_args(args[target_type_index + 2 :]),
            )
        )
    return candidates[-1] if candidates else None


class AdminCommands:
    def __init__(
        self,
        settings: Settings,
        service: ForwardRuleService,
        auth: AdminAuth,
        telegram_listener_getter: Callable[[], TelegramUserListener | None],
        qq_status_getter: Callable[[], str],
        qq_targets_getter: Callable[[], list[QQTargetInfo]],
    ) -> None:
        self.settings = settings
        self.service = service
        self.auth = auth
        self.telegram_listener_getter = telegram_listener_getter
        self.qq_status_getter = qq_status_getter
        self.qq_targets_getter = qq_targets_getter

    async def _deny_if_needed(self, update: Update) -> bool:
        if self.auth.is_allowed(update):
            return False
        if update.effective_message:
            await update.effective_message.reply_text("无权限。")
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        await update.effective_message.reply_text(
            "TGQQ Forwarder 管理命令：\n"
            "/status - 查看运行状态\n"
            "/dialogs [关键词] - 查看或搜索 Telegram 会话\n"
            "/rules - 查看转发规则\n"
            "/qq_targets - 查看已缓存的 QQ 目标 ID\n"
            "/add_rule <名称> <TG会话ID|*> <TG发送者ID|*> "
            "<QQ目标类型> <QQ目标ID> [关键词...] - 新增规则；"
            "名称可含空格，重复规则会合并关键词\n"
            "/del_rule <ID> - 删除规则\n"
            "/enable_rule <ID> - 启用规则\n"
            "/disable_rule <ID> - 禁用规则\n"
            "/logs [数量] - 查看最近转发日志\n"
            "/errors [数量] - 查看最近错误日志\n"
            "/pause - 暂停全部转发\n"
            "/resume - 恢复全部转发"
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        listener = self.telegram_listener_getter()
        telegram_status = "已连接" if listener and listener.is_connected else "未连接"
        paused = await self.service.is_paused()
        total_rules, enabled_rules, total_logs = await self.service.counts()
        await update.effective_message.reply_text(
            "运行状态：\n"
            f"Telegram 用户监听：{telegram_status}\n"
            f"QQ WebSocket：{self.qq_status_getter()}\n"
            f"是否暂停转发：{'是' if paused else '否'}\n"
            f"规则数量：启用 {enabled_rules} / 总计 {total_rules}\n"
            f"日志数量：{total_logs}"
        )

    async def dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        listener = self.telegram_listener_getter()
        if not listener:
            await update.effective_message.reply_text("Telegram 用户监听器尚未就绪。")
            return
        query = " ".join(context.args) if context.args else None
        dialogs = await listener.dialogs.list_dialogs(limit=80, query=query)
        if not dialogs:
            await update.effective_message.reply_text("没有找到匹配的 Telegram 会话。")
            return
        lines = [
            f"{item.type} | <code>{item.id}</code> | {_escape(item.name)}"
            for item in dialogs[:50]
        ]
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        rules = await self.service.list_rules(limit=50)
        if not rules:
            await update.effective_message.reply_text("当前没有转发规则。")
            return
        lines = []
        for rule in rules:
            state = "启用" if rule.enabled else "禁用"
            keywords = keywords_from_text_include_regex(rule.text_include_regex)
            keyword_note = f" 关键词={_escape('、'.join(keywords))}" if keywords else ""
            lines.append(
                f"#{rule.id} [{state}] {_escape(rule.name)} | "
                f"TG会话={rule.source_chat_id or '*'} "
                f"TG发送者={rule.source_sender_id or '*'} "
                f"{keyword_note} -> "
                f"{_escape(rule.qq_target_type)}:{_escape(rule.qq_target_id)}"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def qq_targets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        targets = self.qq_targets_getter()
        if not targets:
            await update.effective_message.reply_text(
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
            "/add_rule qq_to_group -1001234567890 * group 上面查到的目标ID\n"
            "/add_rule qq_to_group_ai -1001234567890 * group 上面查到的目标ID AI,Python"
        )
        await update.effective_message.reply_text(
            "\n".join(lines)[:3900],
            parse_mode=ParseMode.HTML,
        )

    async def add_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        parsed = parse_add_rule_args(context.args)
        if parsed is None:
            await update.effective_message.reply_text(
                "用法：/add_rule <名称> <TG会话ID|*> <TG发送者ID|*> "
                "<QQ目标类型> <QQ目标ID> [关键词...]\n"
                "示例：/add_rule LINUX DO Channel -1002035446470 * c2c "
                "QQ_OPENID gpt,注册机,公益\n"
                "名称可以包含空格；关键词为可选项，可用空格、英文逗号或中文逗号分隔。\n"
                "QQ目标类型可选：group、c2c、channel、dms"
            )
            return
        rule = ForwardRule(
            name=parsed.name,
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
        if result.created:
            prefix = f"已创建规则 #{result.rule.id}"
        elif result.updated:
            prefix = f"已合并到已有规则 #{result.rule.id}"
        else:
            prefix = f"规则已存在 #{result.rule.id}"
        await update.effective_message.reply_text(f"{prefix}。{keyword_note}{duplicate_note}。")

    async def del_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        rule_id = self._first_int_arg(context)
        if rule_id is None:
            await update.effective_message.reply_text("用法：/del_rule <ID>")
            return
        deleted = await self.service.delete_rule(rule_id)
        await update.effective_message.reply_text("已删除。" if deleted else "规则不存在。")

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
            await update.effective_message.reply_text(
                "用法：/enable_rule <ID> 或 /disable_rule <ID>"
            )
            return
        changed = await self.service.set_rule_enabled(rule_id, enabled)
        await update.effective_message.reply_text("已更新。" if changed else "规则不存在。")

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
        await update.effective_message.reply_text("已暂停全部转发。")

    async def resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._deny_if_needed(update):
            return
        await self.service.set_paused(False)
        await update.effective_message.reply_text("已恢复全部转发。")

    async def _send_logs(self, update: Update, rows) -> None:
        if not rows:
            await update.effective_message.reply_text("没有日志。")
            return
        lines = []
        for row in rows:
            lines.append(
                f"#{row.id} 状态={row.status} 规则={row.rule_id} "
                f"TG={row.tg_chat_id}/{row.tg_message_id} -> "
                f"{row.qq_target_type}:{_escape(row.qq_target_id)} "
                f"错误={_escape(row.error_message or '')}"
            )
        text = "\n".join(lines)
        await update.effective_message.reply_text(text[:3900], parse_mode=ParseMode.HTML)

    @staticmethod
    def _first_int_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
        if not context.args:
            return None
        try:
            return int(context.args[0])
        except ValueError:
            return None
