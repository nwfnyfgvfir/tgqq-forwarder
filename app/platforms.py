"""平台常量。

转发链路里每个平台承担两种角色：

* 来源（source）：Telegram 用户账号、个人微信、QQ 官方机器人；
* 目标（target）：QQ 官方机器人会话、个人微信好友、Telegram 会话。

规则用 ``source_platform`` / ``target_platform`` 区分，空值表示「任意来源」。
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    TELEGRAM = "telegram"
    WEIXIN = "weixin"
    QQ = "qq"


SOURCE_PLATFORMS: tuple[str, ...] = (
    Platform.TELEGRAM.value,
    Platform.WEIXIN.value,
    Platform.QQ.value,
)
TARGET_PLATFORMS: tuple[str, ...] = (
    Platform.QQ.value,
    Platform.WEIXIN.value,
    Platform.TELEGRAM.value,
)

# Telegram 目标的发送身份：
# * ``chat``：用已登录的 Telethon 用户账号发送（可发到任意会话，含收藏夹 ``me``）；
# * ``bot``：用 Bot API 发送（Bot 必须已在目标会话内）。
TELEGRAM_TARGET_CHAT = "chat"
TELEGRAM_TARGET_BOT = "bot"
TELEGRAM_TARGET_TYPES: tuple[str, ...] = (TELEGRAM_TARGET_CHAT, TELEGRAM_TARGET_BOT)

PLATFORM_LABELS = {
    Platform.TELEGRAM.value: "Telegram",
    Platform.WEIXIN.value: "微信",
    Platform.QQ.value: "QQ",
}


def platform_label(value: str | None) -> str:
    if not value:
        return "任意"
    return PLATFORM_LABELS.get(value, value)
