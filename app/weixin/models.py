from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from app.platforms import SOURCE_PLATFORMS, Platform

PLATFORM_WEIXIN = Platform.WEIXIN.value
PLATFORM_TELEGRAM = Platform.TELEGRAM.value

SUPPORTED_SOURCE_PLATFORMS = SOURCE_PLATFORMS

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
# 协议里 bot_type=3 对应个人微信扫码入口（与 AstrBot 默认值一致）。
DEFAULT_BOT_TYPE = "3"

# getupdates / sendmessage 返回该错误码表示登录态已失效，需要重新扫码。
SESSION_TIMEOUT_ERRCODE = -14
# sendmessage 返回该错误码时服务端只会给出 "prepare failed"：
# 既可能是连续发送触发限流，也可能是 context_token 过期，需要重试 + 提示用户。
PREPARE_FAILED_ERRCODE = -2


class WeixinItemType(IntEnum):
    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5


class WeixinMessageType(IntEnum):
    USER = 1
    BOT = 2


class WeixinMessageState(IntEnum):
    FINISH = 2


class WeixinUploadMediaType(IntEnum):
    IMAGE = 1
    VIDEO = 2
    FILE = 3


ITEM_TYPE_TO_MEDIA_TYPE = {
    WeixinItemType.IMAGE: "photo",
    WeixinItemType.VOICE: "voice",
    WeixinItemType.FILE: "document",
    WeixinItemType.VIDEO: "video",
}

MEDIA_TYPE_TO_UPLOAD_TYPE = {
    "photo": WeixinUploadMediaType.IMAGE,
    "image": WeixinUploadMediaType.IMAGE,
    "video": WeixinUploadMediaType.VIDEO,
    "voice": WeixinUploadMediaType.FILE,
    "audio": WeixinUploadMediaType.FILE,
    "document": WeixinUploadMediaType.FILE,
    "animation": WeixinUploadMediaType.VIDEO,
    "sticker": WeixinUploadMediaType.IMAGE,
}


class WeixinError(Exception):
    """微信通道基础异常。"""


class WeixinApiError(WeixinError):
    """iLink 接口返回非 0 错误码，或请求本身失败。

    ``transient`` 标记网络类失败（超时/连接错误），调用方可以据此重试。
    """

    def __init__(
        self,
        message: str,
        *,
        errcode: int | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.errcode = errcode
        self.transient = transient


class WeixinSessionExpired(WeixinApiError):
    """登录态失效（errcode = -14），必须重新扫码。"""


class WeixinMissingToken(WeixinError):
    """还没有拿到该联系人的 context_token（对方从未给本号发过消息）。"""


class WeixinPending(WeixinError):
    """消息已挂起，等 token 刷新后自动补发。"""


@dataclass(slots=True)
class PendingWeixinMessage:
    """等待 token 刷新后补发的出站消息。"""

    outbound: WeixinOutboundMessage
    created_at: float = field(default_factory=time.time)
    attempts: int = 0


class WeixinLoginError(WeixinError):
    """扫码登录流程失败。"""


@dataclass(slots=True)
class WeixinCredentials:
    token: str
    account_id: str = ""
    user_id: str = ""
    base_url: str = DEFAULT_BASE_URL
    cdn_base_url: str = DEFAULT_CDN_BASE_URL

    @property
    def logged_in(self) -> bool:
        return bool(self.token)

    def to_dict(self) -> dict[str, str]:
        return {
            "token": self.token,
            "account_id": self.account_id,
            "user_id": self.user_id,
            "base_url": self.base_url,
            "cdn_base_url": self.cdn_base_url,
        }


@dataclass(slots=True)
class WeixinContact:
    user_id: str
    name: str | None = None
    last_seen: float = field(default_factory=time.time)
    has_context_token: bool = False
    # context_token 只能从对方发来的消息里拿到，这里记录它最后一次刷新的时间，
    # 以及最近一次发送被判为失效（errcode=-2）的标记，供管理台提示。
    token_updated_at: float = 0.0
    token_stale: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "last_seen": self.last_seen,
            "has_context_token": self.has_context_token,
            "token_updated_at": self.token_updated_at,
            "token_stale": self.token_stale,
        }


@dataclass(slots=True)
class WeixinOutboundMessage:
    target_user_id: str
    text: str
    media_paths: list[Path] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    # 与 media_paths 一一对应的原始文件名，微信侧用于 file_item.file_name
    media_filenames: list[str] = field(default_factory=list)
    media_caption: str | None = None

    def media_type_at(self, index: int) -> str | None:
        if index < len(self.media_types):
            return self.media_types[index]
        return None

    def media_filename_at(self, index: int) -> str | None:
        if index < len(self.media_filenames):
            name = (self.media_filenames[index] or "").strip()
            return name or None
        return None


@dataclass(slots=True)
class WeixinLoginState:
    """一次扫码登录会话。"""

    qrcode: str
    image_path: Path | None = None
    image_url: str | None = None
    # 真正的扫码内容（通常是 https://liteapp.weixin.qq.com/... 链接）
    scan_url: str | None = None
    status: str = "wait"
    started_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    error: str | None = None
    refresh_count: int = 0
    confirmed: bool = False

    @property
    def pending(self) -> bool:
        return not self.confirmed and self.status not in {"expired", "error"}

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "qrcode": self.qrcode,
            "image_url": self.image_url,
            "scan_url": self.scan_url,
            "expires_at": self.expires_at,
            "error": self.error,
            "refresh_count": self.refresh_count,
            "pending": self.pending,
            "confirmed": self.confirmed,
        }


@dataclass(slots=True)
class WeixinStatus:
    enabled: bool
    logged_in: bool
    connected: bool
    account_id: str
    account_user_id: str | None = None
    base_url: str = DEFAULT_BASE_URL
    contacts: int = 0
    inbound_total: int = 0
    outbound_total: int = 0
    error_total: int = 0
    # 已挂起、等待 token 刷新后补发的消息数
    pending_outbound: int = 0
    last_error: str | None = None
    login: WeixinLoginState | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "logged_in": self.logged_in,
            "connected": self.connected,
            "account_id": self.account_id,
            "account_user_id": self.account_user_id,
            "base_url": self.base_url,
            "contacts": self.contacts,
            "inbound_total": self.inbound_total,
            "outbound_total": self.outbound_total,
            "error_total": self.error_total,
            "pending_outbound": self.pending_outbound,
            "last_error": self.last_error,
            "login": self.login.to_dict() if self.login else None,
        }
