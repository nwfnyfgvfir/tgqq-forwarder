"""跨平台投递：按单个 ``ForwardTarget`` 的 ``platform`` 选择 QQ / 微信 / Telegram 发送器。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import botpy.errors

from app.config import Settings
from app.platforms import Platform
from app.qq_official.models import QQOutboundMessage
from app.qq_official.sender import QQOfficialSender, is_qq_audit_error
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule, ForwardTarget, legacy_target
from app.telegram_outbound.delivery import TelegramDelivery, TelegramDeliveryError
from app.weixin.models import WeixinError, WeixinOutboundMessage, WeixinPending
from app.weixin.service import WeixinService

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """目标平台不可用或目标配置非法。"""


class DeliveryPending(Exception):
    """消息已挂起，等条件满足后自动补发（不算失败）。"""


@dataclass(slots=True)
class DeliveryResult:
    """一次投递的结果。降级发送时 sent_text 是实际发出去的内容。"""

    sent_text: str | None = None
    note: str | None = None


class DeliveryRouter:
    def __init__(
        self,
        settings: Settings,
        qq_sender: QQOfficialSender,
        weixin_service: WeixinService | None = None,
        telegram_delivery: TelegramDelivery | None = None,
    ) -> None:
        self.settings = settings
        self.qq_sender = qq_sender
        self.weixin_service = weixin_service
        self.telegram_delivery = telegram_delivery

    @property
    def target_platforms(self) -> list[str]:
        platforms = [Platform.QQ.value]
        if self.weixin_service is not None and self.weixin_service.enabled:
            platforms.append(Platform.WEIXIN.value)
        if self.telegram_delivery is not None and self.telegram_delivery.enabled:
            platforms.append(Platform.TELEGRAM.value)
        return platforms

    async def deliver(
        self,
        rule: ForwardRule,
        message: TelegramForwardMessage,
        text: str,
        target: ForwardTarget | None = None,
    ) -> DeliveryResult:
        """投递到单个目标；不传 target 时按规则的历史单目标列投递。"""
        resolved = target if target is not None else legacy_target(rule)
        platform = (resolved.platform or Platform.QQ.value).strip()
        if platform == Platform.WEIXIN.value:
            await self._deliver_weixin(rule, resolved, message, text)
            return DeliveryResult()
        if platform == Platform.QQ.value:
            return await self._deliver_qq(rule, resolved, message, text)
        if platform == Platform.TELEGRAM.value:
            await self._deliver_telegram(rule, resolved, message, text)
            return DeliveryResult()
        raise DeliveryError(f"不支持的目标平台：{platform}")

    async def _deliver_qq(
        self,
        rule: ForwardRule,
        target: ForwardTarget,
        message: TelegramForwardMessage,
        text: str,
    ) -> DeliveryResult:
        outbound = QQOutboundMessage(
            target_type=target.target_type,
            target_id=getattr(target, "channel_id", None) or target.target_id,
            text=text,
            media_path=message.media_path,
            media_type=message.media_type,
            media_paths=message.media_paths,
            media_types=message.media_types,
            media_filenames=list(message.media_filenames),
            media_caption=(getattr(rule, "name", "") or "").strip() or "媒体",
            guild_id=target.guild_id,
            channel_id=target.channel_id,
        )
        try:
            await self._call(
                self.qq_sender.send(outbound), self.settings.qq_send_timeout_seconds, "QQ"
            )
            return DeliveryResult()
        except botpy.errors.ServerError as error:
            if not is_qq_audit_error(error) or not self.settings.qq_audit_link_fallback:
                raise
            link = self._fallback_link(message)
            if not link:
                raise
            # 只改发原文链接，不改写正文：被拦的内容不会再以另一种形式塞进 QQ。
            logger.warning(
                "QQ 审核拦截原文 rule=%s target=%s，降级为只发原文链接 %s",
                getattr(rule, "id", None),
                target.label,
                link,
            )
            fallback = QQOutboundMessage(
                target_type=target.target_type,
                target_id=getattr(target, "channel_id", None) or target.target_id,
                text=link,
                guild_id=target.guild_id,
                channel_id=target.channel_id,
            )
            await self._call(
                self.qq_sender.send(fallback), self.settings.qq_send_timeout_seconds, "QQ"
            )
            return DeliveryResult(
                sent_text=link,
                note="原文被 QQ 审核拦截，已降级为只发原文链接",
            )

    @staticmethod
    def _fallback_link(message: TelegramForwardMessage) -> str:
        """优先用网页预览链接，其次正文里的第一个链接。"""
        candidates = [message.webpage_url, *(link.url for link in message.links)]
        for value in candidates:
            url = (value or "").strip()
            if url.startswith(("http://", "https://")):
                return url
        return ""

    async def _deliver_weixin(
        self,
        rule: ForwardRule,
        target: ForwardTarget,
        message: TelegramForwardMessage,
        text: str,
    ) -> None:
        service = self.weixin_service
        if service is None or not service.enabled:
            raise DeliveryError("微信通道未启用，无法投递到微信")
        target_user_id = (target.target_id or "").strip()
        if not target_user_id:
            raise DeliveryError("微信目标用户为空")
        outbound = WeixinOutboundMessage(
            target_user_id=target_user_id,
            text=text,
            media_paths=list(message.media_paths),
            media_types=list(message.media_types),
            media_filenames=list(message.media_filenames),
            media_caption=(getattr(rule, "name", "") or "").strip() or "媒体",
        )
        try:
            await self._call(
                service.send(outbound),
                self.settings.weixin_send_timeout_seconds,
                "微信",
            )
        except WeixinPending as exc:
            raise DeliveryPending(str(exc)) from exc
        except WeixinError as exc:
            raise DeliveryError(str(exc)) from exc

    async def _deliver_telegram(
        self,
        rule: ForwardRule,
        target: ForwardTarget,
        message: TelegramForwardMessage,
        text: str,
    ) -> None:
        delivery = self.telegram_delivery
        if delivery is None or not delivery.enabled:
            raise DeliveryError("Telegram 目标未就绪：需要已连接的 Telegram 账号或已配置的 Bot")
        try:
            await self._call(
                delivery.send(target, message, text),
                self.settings.telegram_send_timeout_seconds,
                "Telegram",
            )
        except TelegramDeliveryError as exc:
            raise DeliveryError(str(exc)) from exc

    @staticmethod
    async def _call(coro, timeout: float | None, label: str) -> None:
        if timeout and timeout > 0:
            try:
                await asyncio.wait_for(coro, timeout=timeout)
            except TimeoutError as exc:
                raise DeliveryError(f"{label} 发送超时（timed out after {timeout}s）") from exc
            return
        await coro
