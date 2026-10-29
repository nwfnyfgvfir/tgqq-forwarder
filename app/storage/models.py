from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, DateTime, Text, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


class FlexibleId(TypeDecorator):
    """会话/发送者 ID。

    Telegram 用整数 ID，个人微信用 wxid 这类字符串，因此列按 TEXT 存储：

    * 写入时统一转成字符串（SQLite 的 INTEGER 亲和性会把纯数字再还原成整数）；
    * 读取时纯数字还原成 ``int``，保证旧库里的 Telegram 规则行为完全不变。
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, int):
            return value
        text = str(value)
        if text.lstrip("-").isdigit() and len(text) <= 18:
            return int(text)
        return text


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
    # 已挂起，等 token 刷新 / 条件满足后自动补发（目前用于微信 token 失效）
    PENDING = "pending"


class ForwardRule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, max_length=120)
    enabled: bool = Field(default=True, index=True)

    # 来源平台：telegram / weixin；None 表示任意平台（历史规则保持此语义）
    source_platform: str | None = Field(default=None, index=True, max_length=32)
    source_account_id: str | None = Field(default=None, index=True, max_length=64)
    source_chat_id: int | str | None = Field(
        default=None,
        sa_column=Column(FlexibleId, nullable=True, index=True),
    )
    source_chat_type: str | None = Field(default=None, max_length=32)
    source_sender_id: int | str | None = Field(
        default=None,
        sa_column=Column(FlexibleId, nullable=True, index=True),
    )
    source_sender_is_bot: bool | None = Field(default=None)

    text_include_regex: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    text_exclude_regex: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    media_types: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    # 目标平台：qq / weixin。下面三列是「目标描述」，weixin 时 qq_target_type 固定 friend、
    # qq_target_id 存好友 wxid / user_id（列名沿用历史命名，避免破坏既有数据与接口）。
    target_platform: str = Field(default="qq", max_length=32, index=True)
    qq_target_type: str = Field(default=QQTargetType.GROUP.value, max_length=32)
    qq_target_id: str = Field(max_length=128)
    qq_guild_id: str | None = Field(default=None, max_length=128)
    qq_channel_id: str | None = Field(default=None, max_length=128)

    message_template: str = Field(sa_column=Column(Text, nullable=False))
    priority: int = Field(default=0, index=True)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def target_id(self) -> str:
        """投递目标 ID（QQ 群 openid / C2C openid / 微信 user_id）。"""
        return self.qq_channel_id or self.qq_target_id

    @property
    def target_type(self) -> str:
        return self.qq_target_type

    @property
    def targets_weixin(self) -> bool:
        return self.target_platform == "weixin"


class ForwardTarget(SQLModel, table=True):
    """一条规则的单个投递目标。

    历史规则把目标写在 ``ForwardRule.target_platform`` / ``qq_target_*`` 上，
    现在一条规则可以同时投递到多个平台、多个目标：那些列继续作为「兼容字段」
    保存第一个目标，``forward_target`` 表保存完整列表。旧规则没有对应行时，
    服务层按兼容字段合成一个目标，行为与升级前完全一致。
    """

    __tablename__ = "forward_target"

    id: int | None = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="forwardrule.id", index=True)
    position: int = Field(default=0, index=True)
    enabled: bool = Field(default=True)
    # 目标平台：qq / weixin
    platform: str = Field(default="qq", max_length=32, index=True)
    # QQ: group / c2c / channel / dms；微信固定 friend；Telegram: chat（用户账号）/ bot（Bot API）
    target_type: str = Field(default=QQTargetType.GROUP.value, max_length=32)
    # QQ 群 openid / C2C openid / 频道 channel_id / 频道私信 guild_id；微信 user_id；
    # Telegram 会话 ID（数字 ID / @username / me）
    target_id: str = Field(max_length=128)
    guild_id: str | None = Field(default=None, max_length=128)
    channel_id: str | None = Field(default=None, max_length=128)
    # 仅 Telegram 目标使用：用哪个 Telethon 账号发送（空 = 默认账号）
    account_id: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def delivery_id(self) -> str:
        """真正用于发送的 ID：频道目标历史上优先用 channel_id。"""
        return self.channel_id or self.target_id

    @property
    def label(self) -> str:
        account = f"@{self.account_id}" if self.account_id else ""
        return f"{self.platform}{account} {self.target_type}:{self.target_id}"


def legacy_target(rule: ForwardRule) -> ForwardTarget:
    """按历史列合成一个目标对象（不落库）。"""
    return ForwardTarget(
        rule_id=int(rule.id or 0),
        platform=rule.target_platform or "qq",
        target_type=rule.qq_target_type,
        target_id=rule.qq_target_id,
        guild_id=rule.qq_guild_id,
        channel_id=rule.qq_channel_id,
    )


class ForwardLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rule_id: int | None = Field(default=None, foreign_key="forwardrule.id", index=True)
    source_platform: str = Field(default="telegram", max_length=32, index=True)
    tg_account_id: str | None = Field(default=None, index=True, max_length=64)
    tg_account_user_id: int | None = Field(default=None, index=True)
    tg_chat_id: int | str | None = Field(
        default=None, sa_column=Column(FlexibleId, nullable=True, index=True)
    )
    tg_message_id: int | str | None = Field(
        default=None, sa_column=Column(FlexibleId, nullable=True, index=True)
    )
    tg_sender_id: int | str | None = Field(
        default=None, sa_column=Column(FlexibleId, nullable=True, index=True)
    )
    tg_chat_title: str | None = Field(default=None, max_length=256)
    tg_sender_name: str | None = Field(default=None, max_length=256)
    target_platform: str = Field(default="qq", max_length=32, index=True)
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
    translate: bool = True
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
    detected_language: str | None = Field(default=None, max_length=16)
    translated_title: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    translated_summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status: str = Field(default="pending", max_length=32, index=True)
    attempts: int = 0
    last_error: str | None = None
    # 仅 status=skipped 时有值，说明为什么没发（未命中关键词/缺链接/首次基线）。
    skip_reason: str | None = Field(default=None, max_length=255)
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
