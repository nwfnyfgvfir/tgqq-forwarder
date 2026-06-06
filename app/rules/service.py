from __future__ import annotations

import logging

from app.rules.formatter import MessageFormatter
from app.rules.matcher import RuleMatcher
from app.rules.models import TelegramForwardMessage
from app.storage.db import Database
from app.storage.models import ForwardLog, ForwardRule, ForwardStatus
from app.storage.repositories import LogRepository, RuleRepository, SettingRepository

logger = logging.getLogger(__name__)


class ForwardRuleService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.rules = RuleRepository()
        self.logs = LogRepository()
        self.settings = SettingRepository()
        self.matcher = RuleMatcher()
        self.formatter = MessageFormatter()

    async def is_paused(self) -> bool:
        async with self.db.session() as session:
            return await self.settings.get_bool(session, "forwarding_paused", default=False)

    async def set_paused(self, paused: bool) -> None:
        async with self.db.session() as session:
            await self.settings.set_bool(session, "forwarding_paused", paused)

    async def matching_rules(self, message: TelegramForwardMessage) -> list[ForwardRule]:
        async with self.db.session() as session:
            rules = await self.rules.list_rules(session, enabled_only=True)
        return [rule for rule in rules if self.matcher.matches(rule, message)]

    async def create_rule(self, rule: ForwardRule) -> ForwardRule:
        async with self.db.session() as session:
            return await self.rules.create_rule(session, rule)

    async def delete_rule(self, rule_id: int) -> bool:
        async with self.db.session() as session:
            return await self.rules.delete_rule(session, rule_id)

    async def set_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        async with self.db.session() as session:
            return await self.rules.set_enabled(session, rule_id, enabled)

    async def list_rules(self, *, enabled_only: bool = False, limit: int | None = None) -> list[ForwardRule]:
        async with self.db.session() as session:
            return await self.rules.list_rules(session, enabled_only=enabled_only, limit=limit)

    async def recent_logs(self, *, limit: int = 20, status: str | None = None) -> list[ForwardLog]:
        async with self.db.session() as session:
            return await self.logs.recent_logs(session, limit=limit, status=status)

    async def counts(self) -> tuple[int, int, int]:
        async with self.db.session() as session:
            total_rules = await self.rules.count_rules(session)
            enabled_rules = await self.rules.count_rules(session, enabled_only=True)
            total_logs = await self.logs.count_logs(session)
        return total_rules, enabled_rules, total_logs

    async def add_forward_log(
        self,
        *,
        rule: ForwardRule | None,
        message: TelegramForwardMessage,
        status: ForwardStatus,
        forwarded_text: str | None = None,
        error_message: str | None = None,
    ) -> None:
        log = ForwardLog(
            rule_id=rule.id if rule else None,
            tg_chat_id=message.chat_id,
            tg_message_id=message.message_id,
            tg_sender_id=message.sender_id,
            tg_chat_title=message.chat_title,
            tg_sender_name=message.sender_name,
            qq_target_type=rule.qq_target_type if rule else "unknown",
            qq_target_id=rule.qq_target_id if rule else "unknown",
            status=status.value,
            error_message=error_message,
            forwarded_text=forwarded_text,
        )
        async with self.db.session() as session:
            await self.logs.add_log(session, log)
