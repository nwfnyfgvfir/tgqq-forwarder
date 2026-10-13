from __future__ import annotations

import re
from string import Formatter
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

DEFAULT_MESSAGE_TEMPLATE = (
    "[{subscription_name}]\n{title}\n{published_at}\n\n{summary}\n\n{translation}\n\n{link}"
)
TEMPLATE_FIELDS = {
    "subscription_name",
    "feed_title",
    "title",
    "summary",
    "link",
    "published_at",
    "translation",
}


def validate_http_url(value: str, *, allow_fragment: bool = False) -> str:
    value = value.strip()
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or (not allow_fragment and "#" in value)
            or "\\" in value
            or "%" in parts.hostname
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError
        _ = parts.port
        parts.hostname.encode("idna")
    except (ValueError, UnicodeError):
        raise ValueError("仅允许无用户信息和 fragment 的有效 HTTP(S) URL") from None
    return value


def validate_template(value: str) -> str:
    try:
        for _, field, spec, conversion in Formatter().parse(value):
            if field is not None and (
                field not in TEMPLATE_FIELDS or spec or conversion is not None
            ):
                raise ValueError
        # 只接受完整的简单占位符或转义花括号，包含空 format_spec 也拒绝。
        fields = "|".join(sorted(TEMPLATE_FIELDS))
        if re.fullmatch(r"(?:[^{}]|\{\{|\}\}|\{(?:" + fields + r")\})*", value) is None:
            raise ValueError
    except ValueError:
        raise ValueError("模板仅允许约定的简单占位符，不允许属性、索引或格式化") from None
    return value


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    feed_url: str = Field(max_length=2048)
    tg_chat_id: str
    enabled: bool = True
    interval_seconds: int = Field(default=600, ge=60, le=86400)
    initial_policy: Literal["skip", "latest"] = "skip"
    initial_count: int = Field(default=3, ge=1, le=20)
    include_keywords: list[Annotated[str, StringConstraints(max_length=120)]] = Field(
        default_factory=list, max_length=100
    )
    exclude_keywords: list[Annotated[str, StringConstraints(max_length=120)]] = Field(
        default_factory=list, max_length=100
    )
    message_template: str = Field(default=DEFAULT_MESSAGE_TEMPLATE, min_length=1, max_length=8000)
    max_summary_length: int = Field(default=600, ge=100, le=2000)
    translate: bool = True

    @field_validator("feed_url", mode="before")
    @classmethod
    def feed_url_valid(cls, value: object) -> object:
        return validate_http_url(value) if isinstance(value, str) else value

    @field_validator("tg_chat_id")
    @classmethod
    def chat_valid(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"-[1-9][0-9]*|@[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("TG 目标必须为负整数或 @username")
        if value.startswith("-") and int(value) < -(2**63):
            raise ValueError("TG 目标整数超出范围")
        return value

    @field_validator("include_keywords", "exclude_keywords")
    @classmethod
    def keywords_valid(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("message_template")
    @classmethod
    def template_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("模板去除空白后不能为空")
        return validate_template(value)
