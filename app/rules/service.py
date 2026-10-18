from __future__ import annotations

import logging
from dataclasses import dataclass

from app.platforms import TELEGRAM_TARGET_CHAT, TELEGRAM_TARGET_TYPES, Platform
from app.rules.formatter import MessageFormatter
from app.rules.keywords import (
    is_keyword_text_include_regex,
    keywords_from_text_include_regex,
    keywords_to_text_include_regex,
)
from app.rules.matcher import RuleMatcher
from app.rules.models import TelegramForwardMessage, id_key
from app.rules.templates import templates_match
from app.storage.db import Database
from app.storage.models import (
    ForwardLog,
    ForwardRule,
    ForwardStatus,
    ForwardTarget,
    legacy_target,
    utc_now,
)
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

    async def targets_for(self, rule: ForwardRule) -> list[ForwardTarget]:
        """规则的启用目标；旧规则没有目标行时按历史列合成一个，行为不变。"""
        if rule.id is None:
            return [legacy_target(rule)]
        async with self.db.session() as session:
            rows = await self.rules.list_targets(session, [int(rule.id)])
        enabled = [row for row in rows if row.enabled]
        if enabled:
            return enabled
        # 目标行全部被停用时不回退到历史列，避免「停用」被静默绕过。
        return [] if rows else [legacy_target(rule)]

    async def target_rows(self, rule_id: int) -> list[ForwardTarget]:
        """规则的全部目标行（含停用），用于后台展示；空列表表示仍是旧单目标规则。"""
        async with self.db.session() as session:
            return await self.rules.list_targets(session, [rule_id])

    async def targets_by_rule(self, rules: list[ForwardRule]) -> dict[int, list[ForwardTarget]]:
        ids = [int(rule.id) for rule in rules if rule.id is not None]
        async with self.db.session() as session:
            rows = await self.rules.list_targets(session, ids)
        grouped: dict[int, list[ForwardTarget]] = {}
        for row in rows:
            grouped.setdefault(row.rule_id, []).append(row)
        result: dict[int, list[ForwardTarget]] = {}
        for rule in rules:
            if rule.id is None:
                continue
            own = grouped.get(int(rule.id))
            result[int(rule.id)] = own if own else [legacy_target(rule)]
        return result

    async def create_rule(self, rule: ForwardRule) -> ForwardRule:
        async with self.db.session() as session:
            return await self.rules.create_rule(session, rule)

    async def create_or_merge_rule(
        self,
        rule: ForwardRule,
        targets: list[ForwardTarget] | None = None,
    ) -> CreateRuleResult:
        requested = self._normalized_targets(rule, targets)
        self._apply_legacy_target(rule, requested)
        async with self.db.session() as session:
            existing_rules = await self.rules.list_rules(session)
            existing_targets = await self.rules.list_targets(
                session, [int(item.id) for item in existing_rules if item.id is not None]
            )
            grouped: dict[int, list[ForwardTarget]] = {}
            for row in existing_targets:
                grouped.setdefault(row.rule_id, []).append(row)
            duplicates = [
                existing
                for existing in existing_rules
                if self._is_same_rule_except_text_include(existing, rule)
                and self._can_merge_text_include(
                    existing.text_include_regex,
                    rule.text_include_regex,
                )
                # 目标不同就是两条规则，不做静默合并（否则会把「多目标」误变成「改目标」）。
                and self._same_target_set(
                    grouped.get(int(existing.id or 0)) or [legacy_target(existing)],
                    requested,
                )
            ]
            if not duplicates:
                created_rule = await self.rules.create_rule(session, rule)
                await self.rules.replace_targets(session, int(created_rule.id or 0), requested)
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
            requested_keys = [self._target_key(item) for item in requested]
            merged_targets = list(requested)
            for duplicate in duplicates:
                dup_rows = await self.rules.list_targets(session, [int(duplicate.id or 0)])
                for row in dup_rows or [legacy_target(duplicate)]:
                    if not any(self._same_target(row, item) for item in merged_targets):
                        merged_targets.append(row)
            for duplicate in duplicates[1:]:
                if duplicate.id is not None and await self.rules.delete_rule(session, duplicate.id):
                    removed_duplicate_count += 1

            await self.rules.replace_targets(session, int(primary.id or 0), merged_targets)
            self._apply_legacy_target(primary, merged_targets)
            session.add(primary)
            await session.flush()
            await session.refresh(primary)
            targets_changed = [self._target_key(item) for item in merged_targets] != requested_keys
            return CreateRuleResult(
                rule=primary,
                created=False,
                updated=pattern_changed or removed_duplicate_count > 0 or targets_changed,
                keywords=keywords_from_text_include_regex(primary.text_include_regex),
                removed_duplicate_count=removed_duplicate_count,
            )

    async def get_rule(self, rule_id: int) -> ForwardRule | None:
        async with self.db.session() as session:
            return await self.rules.get_rule(session, rule_id)

    async def update_rule(
        self,
        rule_id: int,
        values: dict[str, object],
        targets: list[ForwardTarget] | None = None,
    ) -> ForwardRule | None:
        async with self.db.session() as session:
            rule = await self.rules.get_rule(session, rule_id)
            if rule is None:
                return None
            if targets is not None:
                normalized = self._normalized_targets(rule, targets)
                self._apply_legacy_target(rule, normalized)
                values = {**values, **self._legacy_target_values(normalized)}
                await self.rules.replace_targets(session, rule_id, normalized)
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
            source_targets = await self.rules.list_targets(session, [rule_id])
            duplicate = ForwardRule(
                name=name or f"{source.name} 副本",
                enabled=source.enabled if enabled is None else enabled,
                source_platform=source.source_platform,
                source_account_id=source.source_account_id,
                source_chat_id=source.source_chat_id,
                source_chat_type=source.source_chat_type,
                source_sender_id=source.source_sender_id,
                source_sender_is_bot=source.source_sender_is_bot,
                text_include_regex=source.text_include_regex,
                text_exclude_regex=source.text_exclude_regex,
                media_types=list(source.media_types) if source.media_types else None,
                target_platform=source.target_platform,
                qq_target_type=source.qq_target_type,
                qq_target_id=source.qq_target_id,
                qq_guild_id=source.qq_guild_id,
                qq_channel_id=source.qq_channel_id,
                message_template=source.message_template,
                priority=source.priority,
            )
            created = await self.rules.create_rule(session, duplicate)
            await self.rules.replace_targets(
                session,
                int(created.id or 0),
                source_targets or [legacy_target(source)],
            )
            return created

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
    def _target_key(target: ForwardTarget) -> tuple[str, str, str, str]:
        # account_id 只对 Telegram 目标有意义（同一会话可由不同账号发送）。
        return (
            target.platform or "qq",
            target.target_type,
            target.target_id,
            (target.account_id or "") if target.platform == Platform.TELEGRAM.value else "",
        )

    @classmethod
    def _same_target(cls, left: ForwardTarget, right: ForwardTarget) -> bool:
        return cls._target_key(left) == cls._target_key(right)

    @classmethod
    def _same_target_set(cls, left: list[ForwardTarget], right: list[ForwardTarget]) -> bool:
        return sorted(cls._target_key(item) for item in left) == sorted(
            cls._target_key(item) for item in right
        )

    @staticmethod
    def _normalized_targets(
        rule: ForwardRule,
        targets: list[ForwardTarget] | None,
    ) -> list[ForwardTarget]:
        """去空、去重、按平台规整目标；没有可用目标时回退到历史单目标。"""
        if targets:
            rows: list[ForwardTarget] = []
            seen: set[tuple[str, str, str, str]] = set()
            for item in targets:
                platform = (item.platform or "qq").strip()
                target_id = (item.target_id or "").strip()
                if not target_id:
                    continue
                if platform == Platform.WEIXIN.value:
                    target_type = "friend"
                elif platform == Platform.TELEGRAM.value:
                    target_type = (
                        item.target_type
                        if item.target_type in TELEGRAM_TARGET_TYPES
                        else TELEGRAM_TARGET_CHAT
                    )
                else:
                    target_type = item.target_type
                is_weixin = platform == Platform.WEIXIN.value
                is_telegram = platform == Platform.TELEGRAM.value
                account_id = (item.account_id or "").strip() or None
                if is_telegram and target_type != TELEGRAM_TARGET_CHAT:
                    # Bot API 只有一份 token，没有账号维度。
                    account_id = None
                if not is_telegram:
                    account_id = None
                key = (platform, target_type, target_id, account_id or "")
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    ForwardTarget(
                        rule_id=int(rule.id or 0),
                        platform=platform,
                        target_type=target_type,
                        target_id=target_id,
                        guild_id=None if (is_weixin or is_telegram) else (item.guild_id or None),
                        channel_id=(
                            None if (is_weixin or is_telegram) else (item.channel_id or None)
                        ),
                        account_id=account_id,
                        enabled=item.enabled,
                    )
                )
            if rows:
                return rows
        return [legacy_target(rule)]

    @staticmethod
    def _legacy_target_values(targets: list[ForwardTarget]) -> dict[str, object]:
        """兼容列保存第一个目标，旧代码/旧前端读到的仍是有效值。"""
        first = targets[0]
        return {
            "target_platform": first.platform,
            "qq_target_type": first.target_type,
            "qq_target_id": first.target_id,
            "qq_guild_id": first.guild_id,
            "qq_channel_id": first.channel_id,
        }

    @classmethod
    def _apply_legacy_target(cls, rule: ForwardRule, targets: list[ForwardTarget]) -> None:
        for key, value in cls._legacy_target_values(targets).items():
            setattr(rule, key, value)

    @staticmethod
    def _is_same_rule_except_text_include(left: ForwardRule, right: ForwardRule) -> bool:
        return (
            left.name == right.name
            and left.enabled == right.enabled
            and left.source_platform == right.source_platform
            and left.source_account_id == right.source_account_id
            and id_key(left.source_chat_id) == id_key(right.source_chat_id)
            and left.source_chat_type == right.source_chat_type
            and id_key(left.source_sender_id) == id_key(right.source_sender_id)
            and left.source_sender_is_bot == right.source_sender_is_bot
            and left.text_exclude_regex == right.text_exclude_regex
            and left.media_types == right.media_types
            and left.target_platform == right.target_platform
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
        target: ForwardTarget | None = None,
    ) -> None:
        resolved = target if target is not None else (legacy_target(rule) if rule else None)
        log = ForwardLog(
            rule_id=rule.id if rule else None,
            source_platform=message.source_platform,
            tg_account_id=message.account_id,
            tg_account_user_id=message.account_user_id,
            tg_chat_id=message.chat_id,
            tg_message_id=message.message_id,
            tg_sender_id=message.sender_id,
            tg_chat_title=message.chat_title,
            tg_sender_name=message.sender_name,
            target_platform=resolved.platform if resolved else "qq",
            qq_target_type=resolved.target_type if resolved else "unknown",
            qq_target_id=resolved.target_id if resolved else "unknown",
            status=status.value,
            error_message=error_message,
            forwarded_text=forwarded_text,
        )
        async with self.db.session() as session:
            await self.logs.add_log(session, log)
