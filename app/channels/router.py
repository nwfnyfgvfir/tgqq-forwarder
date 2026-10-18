"""跨平台投递：按单个 ``ForwardTarget`` 的 ``platform`` 选择 QQ 或微信发送器。"""

from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.platforms import Platform
from app.qq_official.models import QQOutboundMessage
from app.qq_official.sender import QQOfficialSender
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule, ForwardTarget, legacy_target
from app.weixin.models import WeixinError, WeixinOutboundMessage
from app.weixin.service import WeixinService

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """目标平台不可用或目标配置非法。"""


class DeliveryRouter:
    def __init__(
        self,
        settings: Settings,
        qq_sender: QQOfficialSender,
        weixin_service: WeixinService | None = None,
    ) -> None:
        self.settings = settings
        self.qq_sender = qq_sender
        self.weixin_service = weixin_service

    @property
    def target_platforms(self) -> list[str]:
        platforms = [Platform.QQ.value]
        if self.weixin_service is not None and self.weixin_service.enabled:
            platforms.append(Platform.WEIXIN.value)
        return platforms

    async def deliver(
        self,
        rule: ForwardRule,
        message: TelegramForwardMessage,
        text: str,
        target: ForwardTarget | None = None,
    ) -> None:
        """投递到单个目标；不传 target 时按规则的历史单目标列投递。"""
        resolved = target if target is not None else legacy_target(rule)
        platform = (resolved.platform or Platform.QQ.value).strip()
        if platform == Platform.WEIXIN.value:
            await self._deliver_weixin(rule, resolved, message, text)
            return
        if platform == Platform.QQ.value:
            await self._deliver_qq(rule, resolved, message, text)
            return
        raise DeliveryError(f"不支持的目标平台：{platform}")

    async def _deliver_qq(
        self,
        rule: ForwardRule,
        target: ForwardTarget,
        message: TelegramForwardMessage,
        text: str,
    ) -> None:
        outbound = QQOutboundMessage(
            target_type=target.target_type,
            target_id=getattr(target, "channel_id", None) or target.target_id,
            text=text,
            media_path=message.media_path,
            media_type=message.media_type,
            media_paths=message.media_paths,
            media_types=message.media_types,
            media_caption=(getattr(rule, "name", "") or "").strip() or "媒体",
            guild_id=target.guild_id,
            channel_id=target.channel_id,
        )
        await self._call(self.qq_sender.send(outbound), self.settings.qq_send_timeout_seconds, "QQ")

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
            media_caption=(getattr(rule, "name", "") or "").strip() or "媒体",
        )
        try:
            await self._call(
                service.send(outbound),
                self.settings.weixin_send_timeout_seconds,
                "微信",
            )
        except WeixinError as exc:
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
