from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import AppSetting, ForwardLog, ForwardRule, ForwardStatus


def _now() -> datetime:
    return datetime.now(UTC)


class RuleRepository:
    async def list_rules(
        self,
        session: AsyncSession,
        *,
        enabled_only: bool = False,
        limit: int | None = None,
    ) -> list[ForwardRule]:
        stmt = select(ForwardRule).order_by(ForwardRule.priority.desc(), ForwardRule.id.asc())
        if enabled_only:
            stmt = stmt.where(ForwardRule.enabled.is_(True))
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_rule(self, session: AsyncSession, rule_id: int) -> ForwardRule | None:
        return await session.get(ForwardRule, rule_id)

    async def create_rule(self, session: AsyncSession, rule: ForwardRule) -> ForwardRule:
        rule.created_at = _now()
        rule.updated_at = _now()
        session.add(rule)
        await session.flush()
        await session.refresh(rule)
        return rule

    async def delete_rule(self, session: AsyncSession, rule_id: int) -> bool:
        result = await session.execute(delete(ForwardRule).where(ForwardRule.id == rule_id))
        return bool(result.rowcount)

    async def update_rule(
        self,
        session: AsyncSession,
        rule_id: int,
        values: dict[str, object],
    ) -> ForwardRule | None:
        rule = await self.get_rule(session, rule_id)
        if rule is None:
            return None
        for key, value in values.items():
            if key in {"id", "created_at", "updated_at"}:
                continue
            if hasattr(rule, key):
                setattr(rule, key, value)
        rule.updated_at = _now()
        session.add(rule)
        await session.flush()
        await session.refresh(rule)
        return rule

    async def set_enabled(self, session: AsyncSession, rule_id: int, enabled: bool) -> bool:
        rule = await self.get_rule(session, rule_id)
        if rule is None:
            return False
        rule.enabled = enabled
        rule.updated_at = _now()
        session.add(rule)
        return True

    async def count_rules(self, session: AsyncSession, *, enabled_only: bool = False) -> int:
        stmt = select(func.count()).select_from(ForwardRule)
        if enabled_only:
            stmt = stmt.where(ForwardRule.enabled.is_(True))
        result = await session.execute(stmt)
        return int(result.scalar_one())


class LogRepository:
    async def add_log(self, session: AsyncSession, log: ForwardLog) -> ForwardLog:
        session.add(log)
        await session.flush()
        await session.refresh(log)
        return log

    async def recent_logs(
        self,
        session: AsyncSession,
        *,
        limit: int = 20,
        status: ForwardStatus | str | None = None,
    ) -> list[ForwardLog]:
        stmt = select(ForwardLog).order_by(ForwardLog.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(ForwardLog.status == str(status))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def count_logs(self, session: AsyncSession) -> int:
        result = await session.execute(select(func.count()).select_from(ForwardLog))
        return int(result.scalar_one())


class SettingRepository:
    async def get(self, session: AsyncSession, key: str, default: str | None = None) -> str | None:
        setting = await session.get(AppSetting, key)
        if setting is None:
            return default
        return setting.value

    async def set(self, session: AsyncSession, key: str, value: str) -> None:
        setting = await session.get(AppSetting, key)
        if setting is None:
            setting = AppSetting(key=key, value=value)
        else:
            setting.value = value
            setting.updated_at = _now()
        session.add(setting)

    async def get_bool(self, session: AsyncSession, key: str, default: bool = False) -> bool:
        value = await self.get(session, key, "true" if default else "false")
        return str(value).lower() in {"1", "true", "yes", "on"}

    async def set_bool(self, session: AsyncSession, key: str, value: bool) -> None:
        await self.set(session, key, "true" if value else "false")
