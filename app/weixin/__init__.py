"""个人微信通道（腾讯 iLink / ClawBot 协议）。

与 Telegram / QQ 不同，这里没有 Webhook，也不需要 AppID：
扫码登录拿到 ``bot_token`` 后，用长轮询 ``ilink/bot/getupdates`` 收消息，
用 ``ilink/bot/sendmessage`` 发消息。协议细节参考 AstrBot 的 ``weixin_oc`` 适配器。
"""

from __future__ import annotations

from app.weixin.models import (
    PLATFORM_WEIXIN,
    WeixinApiError,
    WeixinContact,
    WeixinCredentials,
    WeixinError,
    WeixinLoginError,
    WeixinOutboundMessage,
    WeixinSessionExpired,
    WeixinStatus,
)

__all__ = [
    "PLATFORM_WEIXIN",
    "WeixinApiError",
    "WeixinContact",
    "WeixinCredentials",
    "WeixinError",
    "WeixinLoginError",
    "WeixinOutboundMessage",
    "WeixinSessionExpired",
    "WeixinStatus",
]
