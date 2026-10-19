from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.platforms import (
    SOURCE_PLATFORMS,
    TARGET_PLATFORMS,
    TELEGRAM_TARGET_CHAT,
    TELEGRAM_TARGET_TYPES,
    Platform,
)
from app.qq_official.client import QQTargetInfo
from app.rules.keywords import (
    keywords_from_text_include_regex,
    keywords_to_text_include_regex,
    split_keyword_args,
)
from app.rules.templates import effective_message_template
from app.storage.models import ForwardLog, ForwardRule, ForwardTarget, QQTargetType, legacy_target
from app.telegram_user.accounts import TelegramAccountStatus
from app.weixin.models import WeixinContact, WeixinStatus
from app.weixin.qrcode import is_image_bytes

RuleMatchMode = Literal["all", "keywords", "regex"]
WEIXIN_FRIEND_TARGET_TYPE = "friend"


class MiniAppUserResponse(BaseModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    display_name: str


class MeResponse(BaseModel):
    user: MiniAppUserResponse
    auth_date: int


class TelegramAccountStatusResponse(BaseModel):
    id: str
    enabled: bool
    connected: bool
    authorized: bool
    user_id: int | None = None
    username: str | None = None
    phone: str | None = None
    session_path: str
    last_error: str | None = None

    @classmethod
    def from_status(cls, status: TelegramAccountStatus) -> TelegramAccountStatusResponse:
        return cls(
            id=status.id,
            enabled=status.enabled,
            connected=status.connected,
            authorized=status.authorized,
            user_id=status.user_id,
            username=status.username,
            phone=status.phone,
            session_path=status.session_path,
            last_error=status.last_error,
        )


class WeixinLoginResponse(BaseModel):
    status: str
    qrcode: str | None = None
    image_url: str | None = None
    image_data_url: str | None = None
    scan_url: str | None = None
    expires_at: float | None = None
    error: str | None = None
    pending: bool = False
    confirmed: bool = False

    @classmethod
    def from_login(cls, login) -> WeixinLoginResponse:
        if login is None:
            return cls(status="none")
        image_data_url = None
        if login.image_path:
            try:
                payload = login.image_path.read_bytes()
            except OSError:
                payload = b""
            # 只内联真正的图片，避免历史遗留的坏文件把前端 img 弄成裂图。
            if is_image_bytes(payload):
                image_data_url = "data:image/png;base64," + base64.b64encode(payload).decode()
        return cls(
            status=login.status,
            qrcode=login.qrcode,
            image_url=login.image_url,
            image_data_url=image_data_url,
            scan_url=login.scan_url,
            expires_at=login.expires_at or None,
            error=login.error,
            pending=login.pending,
            confirmed=login.confirmed,
        )


class WeixinStatusResponse(BaseModel):
    enabled: bool
    logged_in: bool
    connected: bool
    account_id: str
    account_user_id: str | None = None
    base_url: str
    contacts: int = 0
    inbound_total: int = 0
    outbound_total: int = 0
    error_total: int = 0
    pending_outbound: int = 0
    last_error: str | None = None
    login: WeixinLoginResponse | None = None

    @classmethod
    def from_status(cls, status: WeixinStatus) -> WeixinStatusResponse:
        return cls(
            enabled=status.enabled,
            logged_in=status.logged_in,
            connected=status.connected,
            account_id=status.account_id,
            account_user_id=status.account_user_id,
            base_url=status.base_url,
            contacts=status.contacts,
            inbound_total=status.inbound_total,
            outbound_total=status.outbound_total,
            error_total=status.error_total,
            pending_outbound=status.pending_outbound,
            last_error=status.last_error,
            login=WeixinLoginResponse.from_login(status.login) if status.login else None,
        )


class WeixinContactResponse(BaseModel):
    user_id: str
    name: str | None = None
    last_seen: float | None = None
    has_context_token: bool = False
    token_updated_at: float | None = None
    token_stale: bool = False

    @classmethod
    def from_contact(cls, contact: WeixinContact) -> WeixinContactResponse:
        return cls(
            user_id=contact.user_id,
            name=contact.name,
            last_seen=contact.last_seen or None,
            has_context_token=contact.has_context_token,
            token_updated_at=contact.token_updated_at or None,
            token_stale=contact.token_stale,
        )


class StatusResponse(BaseModel):
    telegram_connected: bool
    telegram_accounts: list[TelegramAccountStatusResponse] = Field(default_factory=list)
    qq_status: str
    qq_enabled: bool = True
    telegram_enabled: bool = True
    weixin: WeixinStatusResponse | None = None
    forwarding_paused: bool
    total_rules: int
    enabled_rules: int
    total_logs: int
    queue_size: int
    queue_max_size: int = 1000
    queue_dropped_total: int = 0
    forward_consumer_alive: bool = True
    forward_consumer_restarts: int = 0
    mini_app_enabled: bool
    mini_app_public_url: str | None = None
    telegram_dedupe_cross_account: bool = False


class PauseRequest(BaseModel):
    paused: bool


class PauseResponse(BaseModel):
    paused: bool


class DialogResponse(BaseModel):
    id: int
    name: str
    type: str
    account_id: str | None = None


class QQTargetResponse(BaseModel):
    target_type: str
    target_id: str
    last_message_id: str | None = None
    display_name: str | None = None
    updated_at: datetime

    @classmethod
    def from_target(cls, target: QQTargetInfo) -> QQTargetResponse:
        return cls(
            target_type=target.target_type,
            target_id=target.target_id,
            last_message_id=target.last_message_id,
            display_name=target.display_name,
            updated_at=target.updated_at,
        )


class RuleTargetInput(BaseModel):
    """单个投递目标；一条规则可以带多个。"""

    platform: str = Platform.QQ.value
    target_type: str = QQTargetType.GROUP.value
    target_id: str = Field(min_length=1, max_length=128)
    guild_id: str | None = None
    channel_id: str | None = None
    # 仅 Telegram 目标使用：用哪个 Telethon 账号发送（空 = 默认账号）
    account_id: str | None = None
    enabled: bool = True

    @field_validator("target_id")
    @classmethod
    def strip_target_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("target_id must not be empty")
        return text

    @field_validator("platform", mode="before")
    @classmethod
    def validate_platform(cls, value: object) -> str:
        text = str(value or Platform.QQ.value).strip().lower()
        if text not in TARGET_PLATFORMS:
            raise ValueError(f"platform must be one of: {', '.join(TARGET_PLATFORMS)}")
        return text

    @field_validator("target_type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        allowed = (
            {item.value for item in QQTargetType}
            | {WEIXIN_FRIEND_TARGET_TYPE}
            | set(TELEGRAM_TARGET_TYPES)
        )
        if value not in allowed:
            raise ValueError(f"target_type must be one of: {', '.join(sorted(allowed))}")
        return value

    @field_validator("guild_id", "channel_id", "account_id")
    @classmethod
    def blank_ids_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def normalize_weixin(self) -> RuleTargetInput:
        if self.platform == Platform.WEIXIN.value:
            self.target_type = WEIXIN_FRIEND_TARGET_TYPE
            self.guild_id = None
            self.channel_id = None
            self.account_id = None
        elif self.platform == Platform.TELEGRAM.value:
            if self.target_type not in TELEGRAM_TARGET_TYPES:
                self.target_type = TELEGRAM_TARGET_CHAT
            self.guild_id = None
            self.channel_id = None
            if self.target_type != TELEGRAM_TARGET_CHAT:
                # Bot API 只有一份 token，没有账号维度。
                self.account_id = None
        else:
            self.account_id = None
        return self

    def to_target(self, rule_id: int = 0) -> ForwardTarget:
        return ForwardTarget(
            rule_id=rule_id,
            platform=self.platform,
            target_type=self.target_type,
            target_id=self.target_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            account_id=self.account_id,
            enabled=self.enabled,
        )


class RuleTargetResponse(BaseModel):
    id: int | None = None
    platform: str = Platform.QQ.value
    target_type: str
    target_id: str
    guild_id: str | None = None
    channel_id: str | None = None
    account_id: str | None = None
    enabled: bool = True

    @classmethod
    def from_target(cls, target: ForwardTarget) -> RuleTargetResponse:
        return cls(
            id=target.id,
            platform=target.platform or Platform.QQ.value,
            target_type=target.target_type,
            target_id=target.target_id,
            guild_id=target.guild_id,
            channel_id=target.channel_id,
            account_id=target.account_id,
            enabled=target.enabled,
        )


class RuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    source_platform: str | None = None
    source_account_id: str | None = None
    source_chat_id: int | str | None = None
    source_chat_type: str | None = None
    source_sender_id: int | str | None = None
    source_sender_is_bot: bool | None = None
    match_mode: RuleMatchMode = "keywords"
    keywords: list[str] = Field(default_factory=list)
    text_include_regex: str | None = None
    text_exclude_regex: str | None = None
    media_types: list[str] | None = None
    target_platform: str = Platform.QQ.value
    qq_target_type: str = QQTargetType.GROUP.value
    qq_target_id: str = Field(default="", max_length=128)
    qq_guild_id: str | None = None
    qq_channel_id: str | None = None
    # 多目标：传了就优先使用，不传则按上面的单目标字段兼容旧客户端。
    targets: list[RuleTargetInput] | None = None
    message_template: str = Field(min_length=1)
    priority: int = 0

    @field_validator("name", "message_template")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value must not be empty")
        return text

    @field_validator("qq_target_id")
    @classmethod
    def strip_target_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_account_id")
    @classmethod
    def blank_account_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("source_platform", mode="before")
    @classmethod
    def validate_source_platform(cls, value: object) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).strip().lower()
        if text not in SOURCE_PLATFORMS:
            raise ValueError(f"source_platform must be one of: {', '.join(SOURCE_PLATFORMS)}")
        return text

    @field_validator("target_platform", mode="before")
    @classmethod
    def validate_target_platform(cls, value: object) -> str:
        text = str(value or Platform.QQ.value).strip().lower()
        if text not in TARGET_PLATFORMS:
            raise ValueError(f"target_platform must be one of: {', '.join(TARGET_PLATFORMS)}")
        return text

    @field_validator("source_chat_type")
    @classmethod
    def validate_chat_type(cls, value: str | None) -> str | None:
        if value in {None, ""}:
            return None
        allowed = {"private", "group", "channel", "unknown"}
        if value not in allowed:
            raise ValueError(f"source_chat_type must be one of: {', '.join(sorted(allowed))}")
        return value

    @field_validator("qq_target_type")
    @classmethod
    def validate_target_type(cls, value: str) -> str:
        allowed = (
            {item.value for item in QQTargetType}
            | {WEIXIN_FRIEND_TARGET_TYPE}
            | set(TELEGRAM_TARGET_TYPES)
        )
        if value not in allowed:
            raise ValueError(f"qq_target_type must be one of: {', '.join(sorted(allowed))}")
        return value

    @field_validator("keywords", mode="before")
    @classmethod
    def normalize_keywords(cls, value: object) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return split_keyword_args([value])
        if isinstance(value, list):
            return split_keyword_args([str(item) for item in value])
        raise ValueError("keywords must be a list or string")

    @field_validator("media_types", mode="before")
    @classmethod
    def normalize_media_types(cls, value: object) -> list[str] | None:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            items = [part.strip() for part in value.split(",")]
        elif isinstance(value, list):
            items = [str(item).strip() for item in value]
        else:
            raise ValueError("media_types must be a list or string")
        cleaned = [item for item in items if item]
        return cleaned or None

    @field_validator("text_include_regex", "text_exclude_regex", "qq_guild_id", "qq_channel_id")
    @classmethod
    def blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def normalize_platform_target(self) -> RuleBase:
        if self.targets is not None:
            self.targets = [item for item in self.targets if item.target_id.strip()]
            if not self.targets:
                # 空列表等同于「没传」，回退到单目标兼容字段（预览等场景会这样传）。
                self.targets = None
        if self.targets is not None:
            first = self.targets[0]
            self.target_platform = first.platform
            self.qq_target_type = first.target_type
            self.qq_target_id = first.target_id
            self.qq_guild_id = first.guild_id
            self.qq_channel_id = first.channel_id
        # 微信目标只有「好友」一种：类型固定 friend，频道相关字段无意义。
        if self.target_platform == Platform.WEIXIN.value:
            self.qq_target_type = WEIXIN_FRIEND_TARGET_TYPE
            self.qq_guild_id = None
            self.qq_channel_id = None
        elif self.target_platform == Platform.TELEGRAM.value:
            if self.qq_target_type not in TELEGRAM_TARGET_TYPES:
                self.qq_target_type = TELEGRAM_TARGET_CHAT
            self.qq_guild_id = None
            self.qq_channel_id = None
            if self.targets is None and not self.qq_target_id:
                raise ValueError("Telegram 目标需要填写会话 ID 或 @username")
        elif self.targets is None and not self.qq_target_id:
            raise ValueError("qq_target_id 不能为空，或用 targets 指定目标")
        if self.source_platform == Platform.WEIXIN.value and self.source_chat_type:
            if self.source_chat_type != "private":
                raise ValueError("微信来源只有私聊，source_chat_type 必须为空或 private")
        return self

    def to_targets(self) -> list[ForwardTarget]:
        if self.targets:
            return [item.to_target() for item in self.targets]
        return [legacy_target(self.to_rule())]

    def to_rule_values(self) -> dict[str, Any]:
        include_regex: str | None
        if self.match_mode == "keywords":
            include_regex = keywords_to_text_include_regex(self.keywords) if self.keywords else None
        elif self.match_mode == "regex":
            include_regex = self.text_include_regex
        else:
            include_regex = None
        return {
            "name": self.name,
            "enabled": self.enabled,
            "source_platform": self.source_platform,
            "source_account_id": self.source_account_id,
            "source_chat_id": self.source_chat_id,
            "source_chat_type": self.source_chat_type,
            "source_sender_id": self.source_sender_id,
            "source_sender_is_bot": self.source_sender_is_bot,
            "text_include_regex": include_regex,
            "text_exclude_regex": self.text_exclude_regex,
            "media_types": self.media_types,
            "target_platform": self.target_platform,
            "qq_target_type": self.qq_target_type,
            "qq_target_id": self.qq_target_id,
            "qq_guild_id": self.qq_guild_id,
            "qq_channel_id": self.qq_channel_id,
            "message_template": self.message_template,
            "priority": self.priority,
        }

    def to_rule(self) -> ForwardRule:
        return ForwardRule(**self.to_rule_values())


class RuleCreateRequest(RuleBase):
    pass


class RuleUpdateRequest(RuleBase):
    pass


class RuleEnabledRequest(BaseModel):
    enabled: bool


class RuleDuplicateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class RuleResponse(BaseModel):
    id: int
    name: str
    enabled: bool
    source_platform: str | None = None
    source_account_id: str | None = None
    source_chat_id: int | str | None = None
    source_chat_type: str | None = None
    source_sender_id: int | str | None = None
    source_sender_is_bot: bool | None = None
    match_mode: RuleMatchMode
    keywords: list[str]
    text_include_regex: str | None = None
    text_exclude_regex: str | None = None
    media_types: list[str] | None = None
    target_platform: str = Platform.QQ.value
    qq_target_type: str
    qq_target_id: str
    qq_guild_id: str | None = None
    qq_channel_id: str | None = None
    targets: list[RuleTargetResponse] = Field(default_factory=list)
    message_template: str
    priority: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_rule(
        cls,
        rule: ForwardRule,
        targets: list[ForwardTarget] | None = None,
    ) -> RuleResponse:
        keywords = keywords_from_text_include_regex(rule.text_include_regex)
        if keywords:
            match_mode: RuleMatchMode = "keywords"
        elif rule.text_include_regex:
            match_mode = "regex"
        else:
            match_mode = "all"
        rows = targets if targets else [legacy_target(rule)]
        return cls(
            id=int(rule.id or 0),
            name=rule.name,
            enabled=rule.enabled,
            source_platform=rule.source_platform,
            source_account_id=rule.source_account_id,
            source_chat_id=rule.source_chat_id,
            source_chat_type=rule.source_chat_type,
            source_sender_id=rule.source_sender_id,
            source_sender_is_bot=rule.source_sender_is_bot,
            match_mode=match_mode,
            keywords=keywords,
            text_include_regex=rule.text_include_regex,
            text_exclude_regex=rule.text_exclude_regex,
            media_types=rule.media_types,
            target_platform=rule.target_platform or Platform.QQ.value,
            qq_target_type=rule.qq_target_type,
            qq_target_id=rule.qq_target_id,
            qq_guild_id=rule.qq_guild_id,
            qq_channel_id=rule.qq_channel_id,
            targets=[RuleTargetResponse.from_target(item) for item in rows],
            message_template=effective_message_template(rule.message_template),
            priority=rule.priority,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )


class RuleCreateResponse(BaseModel):
    rule: RuleResponse
    created: bool
    updated: bool
    keywords: list[str]
    removed_duplicate_count: int = 0


class PreviewMessageRequest(BaseModel):
    text: str = "AI news from Telegram"
    source_platform: str = Platform.TELEGRAM.value
    account_id: str | None = None
    chat_id: int | str | None = -1001234567890
    chat_title: str | None = "Telegram Channel"
    chat_type: str = "channel"
    sender_id: int | str | None = 42
    sender_username: str | None = "sender"
    sender_display_name: str | None = "Sender"
    sender_is_bot: bool = False
    media_type: str | None = None
    links: list[dict[str, str]] = Field(default_factory=list)


class RulePreviewRequest(BaseModel):
    rule: RuleBase
    message: PreviewMessageRequest = Field(default_factory=PreviewMessageRequest)


class RulePreviewResponse(BaseModel):
    matches: bool
    rendered_text: str
    detected_keywords: list[str]
    warnings: list[str] = Field(default_factory=list)


class ForwardLogResponse(BaseModel):
    id: int
    rule_id: int | None = None
    source_platform: str = Platform.TELEGRAM.value
    tg_account_id: str | None = None
    tg_account_user_id: int | None = None
    tg_chat_id: int | str | None = None
    tg_message_id: int | str | None = None
    tg_sender_id: int | str | None = None
    tg_chat_title: str | None = None
    tg_sender_name: str | None = None
    target_platform: str = Platform.QQ.value
    qq_target_type: str
    qq_target_id: str
    status: str
    error_message: str | None = None
    forwarded_text: str | None = None
    created_at: datetime

    @classmethod
    def from_log(cls, log: ForwardLog) -> ForwardLogResponse:
        return cls(
            id=int(log.id or 0),
            rule_id=log.rule_id,
            source_platform=log.source_platform or Platform.TELEGRAM.value,
            tg_account_id=log.tg_account_id,
            tg_account_user_id=log.tg_account_user_id,
            tg_chat_id=log.tg_chat_id,
            tg_message_id=log.tg_message_id,
            tg_sender_id=log.tg_sender_id,
            tg_chat_title=log.tg_chat_title,
            tg_sender_name=log.tg_sender_name,
            target_platform=log.target_platform or Platform.QQ.value,
            qq_target_type=log.qq_target_type,
            qq_target_id=log.qq_target_id,
            status=log.status,
            error_message=log.error_message,
            forwarded_text=log.forwarded_text,
            created_at=log.created_at,
        )


class OptionsResponse(BaseModel):
    qq_target_types: list[str]
    chat_types: list[str]
    media_types: list[str]
    template_variables: list[str]
    telegram_account_ids: list[str] = Field(default_factory=list)
    source_platforms: list[str] = Field(default_factory=lambda: list(SOURCE_PLATFORMS))
    target_platforms: list[str] = Field(default_factory=lambda: list(TARGET_PLATFORMS))
    telegram_target_types: list[str] = Field(
        default_factory=lambda: list(TELEGRAM_TARGET_TYPES)
    )
    telegram_bot_configured: bool = False
    weixin_enabled: bool = False
    weixin_account_id: str | None = None
    weixin_logged_in: bool = False
    weixin_contacts: list[WeixinContactResponse] = Field(default_factory=list)
    now: datetime = Field(default_factory=lambda: datetime.now(UTC))
