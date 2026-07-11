from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, Text
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


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=120)
    value: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: datetime = Field(default_factory=utc_now)
