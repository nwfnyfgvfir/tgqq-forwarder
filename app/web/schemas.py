from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.qq_official.client import QQTargetInfo
from app.rules.keywords import keywords_from_text_include_regex, keywords_to_text_include_regex
from app.storage.models import ForwardLog, ForwardRule, QQTargetType

RuleMatchMode = Literal["all", "keywords", "regex"]


class MiniAppUserResponse(BaseModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    display_name: str


class MeResponse(BaseModel):
    user: MiniAppUserResponse
    auth_date: int


class StatusResponse(BaseModel):
    telegram_connected: bool
    qq_status: str
    forwarding_paused: bool
    total_rules: int
    enabled_rules: int
    total_logs: int
    queue_size: int
    mini_app_enabled: bool
    mini_app_public_url: str | None = None


class PauseRequest(BaseModel):
    paused: bool


class PauseResponse(BaseModel):
    paused: bool


class DialogResponse(BaseModel):
    id: int
    name: str
    type: str


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


class RuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    source_chat_id: int | None = None
    source_chat_type: str | None = None
    source_sender_id: int | None = None
    source_sender_is_bot: bool | None = None
    match_mode: RuleMatchMode = "keywords"
    keywords: list[str] = Field(default_factory=list)
    text_include_regex: str | None = None
    text_exclude_regex: str | None = None
    media_types: list[str] | None = None
    qq_target_type: str = QQTargetType.GROUP.value
    qq_target_id: str = Field(min_length=1, max_length=128)
    qq_guild_id: str | None = None
    qq_channel_id: str | None = None
    message_template: str = Field(min_length=1)
    priority: int = 0

    @field_validator("name", "qq_target_id", "message_template")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value must not be empty")
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
        allowed = {item.value for item in QQTargetType}
        if value not in allowed:
            raise ValueError(f"qq_target_type must be one of: {', '.join(sorted(allowed))}")
        return value

    @field_validator("keywords", mode="before")
    @classmethod
    def normalize_keywords(cls, value: object) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            raw_values = value.replace("\n", ",").split(",")
        elif isinstance(value, list):
            raw_values = [str(item) for item in value]
        else:
            raise ValueError("keywords must be a list or string")
        keywords: list[str] = []
        seen: set[str] = set()
        for raw in raw_values:
            for part in raw.replace("，", ",").replace(";", ",").replace("；", ",").split(","):
                keyword = part.strip()
                if keyword and keyword not in seen:
                    seen.add(keyword)
                    keywords.append(keyword)
        return keywords

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
            "source_chat_id": self.source_chat_id,
            "source_chat_type": self.source_chat_type,
            "source_sender_id": self.source_sender_id,
            "source_sender_is_bot": self.source_sender_is_bot,
            "text_include_regex": include_regex,
            "text_exclude_regex": self.text_exclude_regex,
            "media_types": self.media_types,
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
    source_chat_id: int | None = None
    source_chat_type: str | None = None
    source_sender_id: int | None = None
    source_sender_is_bot: bool | None = None
    match_mode: RuleMatchMode
    keywords: list[str]
    text_include_regex: str | None = None
    text_exclude_regex: str | None = None
    media_types: list[str] | None = None
    qq_target_type: str
    qq_target_id: str
    qq_guild_id: str | None = None
    qq_channel_id: str | None = None
    message_template: str
    priority: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_rule(cls, rule: ForwardRule) -> RuleResponse:
        keywords = keywords_from_text_include_regex(rule.text_include_regex)
        if keywords:
            match_mode: RuleMatchMode = "keywords"
        elif rule.text_include_regex:
            match_mode = "regex"
        else:
            match_mode = "all"
        return cls(
            id=int(rule.id or 0),
            name=rule.name,
            enabled=rule.enabled,
            source_chat_id=rule.source_chat_id,
            source_chat_type=rule.source_chat_type,
            source_sender_id=rule.source_sender_id,
            source_sender_is_bot=rule.source_sender_is_bot,
            match_mode=match_mode,
            keywords=keywords,
            text_include_regex=rule.text_include_regex,
            text_exclude_regex=rule.text_exclude_regex,
            media_types=rule.media_types,
            qq_target_type=rule.qq_target_type,
            qq_target_id=rule.qq_target_id,
            qq_guild_id=rule.qq_guild_id,
            qq_channel_id=rule.qq_channel_id,
            message_template=rule.message_template,
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
    chat_id: int | None = -1001234567890
    chat_title: str | None = "Telegram Channel"
    chat_type: str = "channel"
    sender_id: int | None = 42
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
    tg_chat_id: int | None = None
    tg_message_id: int | None = None
    tg_sender_id: int | None = None
    tg_chat_title: str | None = None
    tg_sender_name: str | None = None
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
            tg_chat_id=log.tg_chat_id,
            tg_message_id=log.tg_message_id,
            tg_sender_id=log.tg_sender_id,
            tg_chat_title=log.tg_chat_title,
            tg_sender_name=log.tg_sender_name,
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
    now: datetime = Field(default_factory=lambda: datetime.now(UTC))
