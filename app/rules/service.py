from __future__ import annotations

import logging
from dataclasses import dataclass

from app.rules.formatter import MessageFormatter
from app.rules.keywords import (
    is_keyword_text_include_regex,
    keywords_from_text_include_regex,
    keywords_to_text_include_regex,
)
from app.rules.matcher import RuleMatcher
from app.rules.models import TelegramForwardMessage
from app.rules.templates import templates_match
from app.storage.db import Database
from app.storage.models import ForwardLog, ForwardRule, ForwardStatus, utc_now
from app.storage.repositories import LogRepository, RuleRepository, SettingRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CreateRuleResult:
    rule: ForwardRule
    created: bool
    updated: bool
    keywords: list[str]
    removed_duplicate_count: int = 0


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

    async def create_or_merge_rule(self, rule: ForwardRule) -> CreateRuleResult:
        async with self.db.session() as session:
            existing_rules = await self.rules.list_rules(session)
            duplicates = [
                existing
                for existing in existing_rules
                if self._is_same_rule_except_text_include(existing, rule)
                and self._can_merge_text_include(
                    existing.text_include_regex,
                    rule.text_include_regex,
                )
            ]
            if not duplicates:
                created_rule = await self.rules.create_rule(session, rule)
                return CreateRuleResult(
                    rule=created_rule,
                    created=True,
                    updated=False,
                    keywords=keywords_from_text_include_regex(created_rule.text_include_regex),
                )

            primary = duplicates[0]
            include_patterns = [duplicate.text_include_regex for duplicate in duplicates]
            include_patterns.append(rule.text_include_regex)
            merged_pattern = self._merged_text_include_regex(include_patterns)
            pattern_changed = primary.text_include_regex != merged_pattern
            if pattern_changed:
                primary.text_include_regex = merged_pattern
                primary.updated_at = utc_now()
                session.add(primary)

            removed_duplicate_count = 0
            for duplicate in duplicates[1:]:
                if duplicate.id is not None and await self.rules.delete_rule(session, duplicate.id):
                    removed_duplicate_count += 1

            await session.flush()
            await session.refresh(primary)
            return CreateRuleResult(
                rule=primary,
                created=False,
                updated=pattern_changed or removed_duplicate_count > 0,
                keywords=keywords_from_text_include_regex(primary.text_include_regex),
                removed_duplicate_count=removed_duplicate_count,
            )

    async def get_rule(self, rule_id: int) -> ForwardRule | None:
        async with self.db.session() as session:
            return await self.rules.get_rule(session, rule_id)

    async def update_rule(self, rule_id: int, values: dict[str, object]) -> ForwardRule | None:
        async with self.db.session() as session:
            return await self.rules.update_rule(session, rule_id, values)

    async def duplicate_rule(
        self,
        rule_id: int,
        *,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> ForwardRule | None:
        async with self.db.session() as session:
            source = await self.rules.get_rule(session, rule_id)
            if source is None:
                return None
            duplicate = ForwardRule(
                name=name or f"{source.name} 副本",
                enabled=source.enabled if enabled is None else enabled,
                source_account_id=source.source_account_id,
                source_chat_id=source.source_chat_id,
                source_chat_type=source.source_chat_type,
                source_sender_id=source.source_sender_id,
                source_sender_is_bot=source.source_sender_is_bot,
                text_include_regex=source.text_include_regex,
                text_exclude_regex=source.text_exclude_regex,
                media_types=list(source.media_types) if source.media_types else None,
                qq_target_type=source.qq_target_type,
                qq_target_id=source.qq_target_id,
                qq_guild_id=source.qq_guild_id,
                qq_channel_id=source.qq_channel_id,
                message_template=source.message_template,
                priority=source.priority,
            )
            return await self.rules.create_rule(session, duplicate)

    async def delete_rule(self, rule_id: int) -> bool:
        async with self.db.session() as session:
            return await self.rules.delete_rule(session, rule_id)

    async def set_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        async with self.db.session() as session:
            return await self.rules.set_enabled(session, rule_id, enabled)

    async def list_rules(
        self,
        *,
        enabled_only: bool = False,
        limit: int | None = None,
    ) -> list[ForwardRule]:
        async with self.db.session() as session:
            return await self.rules.list_rules(session, enabled_only=enabled_only, limit=limit)

    async def recent_logs(self, *, limit: int = 20, status: str | None = None) -> list[ForwardLog]:
        async with self.db.session() as session:
            return await self.logs.recent_logs(session, limit=limit, status=status)

    @staticmethod
    def _is_same_rule_except_text_include(left: ForwardRule, right: ForwardRule) -> bool:
        return (
            left.name == right.name
            and left.enabled == right.enabled
            and left.source_account_id == right.source_account_id
            and left.source_chat_id == right.source_chat_id
            and left.source_chat_type == right.source_chat_type
            and left.source_sender_id == right.source_sender_id
            and left.source_sender_is_bot == right.source_sender_is_bot
            and left.text_exclude_regex == right.text_exclude_regex
            and left.media_types == right.media_types
            and left.qq_target_type == right.qq_target_type
            and left.qq_target_id == right.qq_target_id
            and left.qq_guild_id == right.qq_guild_id
            and left.qq_channel_id == right.qq_channel_id
            and templates_match(left.message_template, right.message_template)
            and left.priority == right.priority
        )

    @staticmethod
    def _can_merge_text_include(left: str | None, right: str | None) -> bool:
        return (left is None or is_keyword_text_include_regex(left)) and (
            right is None or is_keyword_text_include_regex(right)
        )

    @staticmethod
    def _merged_text_include_regex(patterns: list[str | None]) -> str | None:
        keywords: list[str] = []
        seen: set[str] = set()
        has_no_keyword_rule = False
        for pattern in patterns:
            if pattern is None:
                has_no_keyword_rule = True
                continue
            for keyword in keywords_from_text_include_regex(pattern):
                if keyword not in seen:
                    seen.add(keyword)
                    keywords.append(keyword)
        if has_no_keyword_rule:
            return None
        return keywords_to_text_include_regex(keywords) if keywords else None

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
            tg_account_id=message.account_id,
            tg_account_user_id=message.account_user_id,
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
