from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, DateTime, Text, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


class ChatType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


class QQTargetType(StrEnum):
    GROUP = "group"
    C2C = "c2c"
    CHANNEL = "channel"
    DMS = "dms"


class ForwardStatus(StrEnum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


class ForwardRule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, max_length=120)
    enabled: bool = Field(default=True, index=True)

    source_account_id: str | None = Field(default=None, index=True, max_length=64)
    source_chat_id: int | None = Field(default=None, index=True)
    source_chat_type: str | None = Field(default=None, max_length=32)
    source_sender_id: int | None = Field(default=None, index=True)
    source_sender_is_bot: bool | None = Field(default=None)

    text_include_regex: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    text_exclude_regex: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    media_types: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    qq_target_type: str = Field(default=QQTargetType.GROUP.value, max_length=32)
    qq_target_id: str = Field(max_length=128)
    qq_guild_id: str | None = Field(default=None, max_length=128)
    qq_channel_id: str | None = Field(default=None, max_length=128)

    message_template: str = Field(sa_column=Column(Text, nullable=False))
    priority: int = Field(default=0, index=True)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ForwardLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rule_id: int | None = Field(default=None, foreign_key="forwardrule.id", index=True)
    tg_account_id: str | None = Field(default=None, index=True, max_length=64)
    tg_account_user_id: int | None = Field(default=None, index=True)
    tg_chat_id: int | None = Field(default=None, index=True)
    tg_message_id: int | None = Field(default=None, index=True)
    tg_sender_id: int | None = Field(default=None, index=True)
    tg_chat_title: str | None = Field(default=None, max_length=256)
    tg_sender_name: str | None = Field(default=None, max_length=256)
    qq_target_type: str = Field(max_length=32)
    qq_target_id: str = Field(max_length=128)
    status: str = Field(default=ForwardStatus.SUCCESS.value, max_length=32, index=True)
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    forwarded_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=utc_now, index=True)


class RssUTCDateTime(TypeDecorator):
    """SQLite 不保存时区，RSS 表统一按 UTC 读写并返回 aware datetime。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RssSubscription(SQLModel, table=True):
    __tablename__ = "rss_subscription"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=120)
    feed_url: str = Field(max_length=2048)
    tg_chat_id: str = Field(max_length=128)
    enabled: bool = Field(default=True, index=True)
    interval_seconds: int = 600
    initial_policy: str = "skip"
    initial_count: int = 3
    include_keywords: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    exclude_keywords: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    message_template: str = Field(sa_column=Column(Text, nullable=False))
    max_summary_length: int = 600
    initialized: bool = False
    feed_title: str = ""
    feed_version: int = 0
    etag: str | None = None
    last_modified: str | None = None
    last_checked_at: datetime | None = Field(default=None, sa_column=Column(RssUTCDateTime()))
    last_success_at: datetime | None = Field(default=None, sa_column=Column(RssUTCDateTime()))
    last_error: str | None = None
    failure_count: int = 0
    next_check_at: datetime | None = Field(
        default=None, sa_column=Column(RssUTCDateTime(), index=True)
    )
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(RssUTCDateTime(), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(RssUTCDateTime(), nullable=False)
    )


class RssEntry(SQLModel, table=True):
    __tablename__ = "rss_entry"
    __table_args__ = (UniqueConstraint("subscription_id", "entry_key", name="uq_rss_entry_key"),)

    id: int | None = Field(default=None, primary_key=True)
    subscription_id: int = Field(foreign_key="rss_subscription.id", index=True)
    entry_key: str = Field(max_length=256)
    feed_title: str = ""
    title: str = Field(sa_column=Column(Text, nullable=False))
    link: str = Field(sa_column=Column(Text, nullable=False))
    summary: str = Field(sa_column=Column(Text, nullable=False))
    published_at: datetime | None = Field(default=None, sa_column=Column(RssUTCDateTime()))
    status: str = Field(default="pending", max_length=32, index=True)
    attempts: int = 0
    last_error: str | None = None
    next_retry_at: datetime | None = Field(
        default=None, sa_column=Column(RssUTCDateTime(), index=True)
    )
    tg_message_id: int | None = None
    tg_chat_id: int | None = None
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(RssUTCDateTime(), nullable=False)
    )
    sent_at: datetime | None = Field(default=None, sa_column=Column(RssUTCDateTime()))


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=120)
    value: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: datetime = Field(default_factory=utc_now)
