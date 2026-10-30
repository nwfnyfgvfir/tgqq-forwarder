from __future__ import annotations

import asyncio
import logging
import time

from app.channels.router import DeliveryError, DeliveryPending, DeliveryRouter
from app.config import Settings
from app.platforms import Platform
from app.qq_official.sender import QQOfficialSender
from app.rules.models import TelegramForwardMessage, id_key
from app.rules.service import ForwardRuleService
from app.storage.models import ForwardRule, ForwardStatus, ForwardTarget
from app.weixin.service import WeixinService

logger = logging.getLogger(__name__)


class ForwardQueue:
    def __init__(
        self,
        settings: Settings,
        service: ForwardRuleService,
        qq_sender: QQOfficialSender,
        weixin_service: WeixinService | None = None,
        *,
        router: DeliveryRouter | None = None,
    ) -> None:
        self.settings = settings
        self.service = service
        self.qq_sender = qq_sender
        self.weixin_service = weixin_service
        self.router = router or DeliveryRouter(settings, qq_sender, weixin_service)
        self.queue: asyncio.Queue[TelegramForwardMessage] = asyncio.Queue(
            maxsize=settings.forward_queue_size
        )
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._recent_message_keys: dict[tuple[object, object], float] = {}
        self._dedupe_ttl_seconds = 300.0
        self._dropped_total = 0
        self._processed_total = 0
        self._error_total = 0
        self._restart_total = 0
        self._last_error: str | None = None
        self._high_water_warned = False

    @property
    def size(self) -> int:
        return self.queue.qsize()

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def dropped_total(self) -> int:
        return self._dropped_total

    @property
    def restart_total(self) -> int:
        return self._restart_total

    def status_snapshot(self) -> dict[str, object]:
        return {
            "queue_size": self.size,
            "queue_max_size": self.settings.forward_queue_size,
            "queue_dropped_total": self._dropped_total,
            "forward_consumer_alive": self.alive,
            "forward_consumer_restarts": self._restart_total,
            "forward_processed_total": self._processed_total,
            "forward_error_total": self._error_total,
            "forward_last_error": self._last_error,
        }

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_supervisor(), name="forward-queue")
        logger.info("Forward queue started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Forward queue stopped")

    async def enqueue(self, message: TelegramForwardMessage) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self._dropped_total += 1
            logger.error(
                "Forward queue is full; dropping Telegram message "
                "account=%s message=%s chat=%s "
                "queue_size=%s max=%s dropped_total=%s consumer_alive=%s",
                message.account_id,
                message.message_id,
                message.chat_id,
                self.queue.qsize(),
                self.settings.forward_queue_size,
                self._dropped_total,
                self.alive,
            )
            return

        max_size = self.settings.forward_queue_size
        if max_size <= 0:
            return
        current = self.queue.qsize()
        high_water = max(1, int(max_size * 0.8))
        low_water = max(0, int(max_size * 0.5))
        if current >= high_water and not self._high_water_warned:
            self._high_water_warned = True
            logger.warning(
                "Forward queue high water queue_size=%s max=%s consumer_alive=%s",
                current,
                max_size,
                self.alive,
            )
        elif current <= low_water and self._high_water_warned:
            self._high_water_warned = False

    async def _run_supervisor(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._run()
                if self._stopping.is_set():
                    return
                logger.error("Forward queue consumer loop exited unexpectedly; restarting")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Forward queue consumer crashed; restarting")
            self._restart_total += 1
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise

    async def _run(self) -> None:
        while not self._stopping.is_set():
            message = await self.queue.get()
            try:
                await self._process_message(message)
                self._processed_total += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error_total += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "Unhandled error processing Telegram message account=%s message=%s chat=%s",
                    message.account_id,
                    message.message_id,
                    message.chat_id,
                )
            finally:
                self.queue.task_done()

    def _should_skip_cross_account_duplicate(self, message: TelegramForwardMessage) -> bool:
        if not self.settings.telegram_dedupe_cross_account:
            return False
        if message.chat_id is None:
            return False
        now = time.monotonic()
        expired = [
            key
            for key, seen_at in self._recent_message_keys.items()
            if now - seen_at > self._dedupe_ttl_seconds
        ]
        for key in expired:
            self._recent_message_keys.pop(key, None)

        key = (message.chat_id, message.message_id)
        if key in self._recent_message_keys:
            logger.info(
                "Skip cross-account duplicate Telegram message account=%s chat=%s message=%s",
                message.account_id,
                message.chat_id,
                message.message_id,
            )
            return True
        self._recent_message_keys[key] = now
        return False

    async def _safe_add_forward_log(
        self,
        *,
        rule: ForwardRule | None,
        message: TelegramForwardMessage,
        status: ForwardStatus,
        forwarded_text: str | None = None,
        error_message: str | None = None,
        target: ForwardTarget | None = None,
    ) -> None:
        try:
            await self.service.add_forward_log(
                rule=rule,
                message=message,
                status=status,
                forwarded_text=forwarded_text,
                error_message=error_message,
                target=target,
            )
        except Exception:
            logger.exception(
                "Failed to write forward log rule=%s account=%s message=%s status=%s",
                getattr(rule, "id", None),
                message.account_id,
                message.message_id,
                status.value,
            )

    async def _process_message(self, message: TelegramForwardMessage) -> None:
        if await self.service.is_paused():
            logger.info(
                "Forwarding is paused; skip Telegram message account=%s message=%s",
                message.account_id,
                message.message_id,
            )
            return

        if self._should_skip_cross_account_duplicate(message):
            return

        rules = await self.service.matching_rules(message)
        if not rules:
            logger.debug(
                "No rule matched account=%s chat=%s message=%s sender=%s",
                message.account_id,
                message.chat_id,
                message.message_id,
                message.sender_id,
            )
            return

        for rule in rules:
            forwarded_text = self.service.formatter.format(rule, message)
            targets = await self.service.targets_for(rule)
            if not targets:
                logger.info("Rule %s has no enabled target; skip", rule.id)
                continue
            for target in targets:
                await self._deliver_one(rule, message, forwarded_text, target)

    async def _deliver_one(
        self,
        rule: ForwardRule,
        message: TelegramForwardMessage,
        forwarded_text: str,
        target: ForwardTarget,
    ) -> None:
        """单个目标失败不影响其它目标，每个目标单独记一条日志。"""
        if self._is_echo_target(message, target):
            logger.info(
                "Skip forwarding %s message %s back to its own chat %s by rule %s",
                message.source_platform,
                message.message_id,
                message.chat_id,
                rule.id,
            )
            await self._safe_add_forward_log(
                rule=rule,
                message=message,
                status=ForwardStatus.SKIPPED,
                forwarded_text=forwarded_text,
                error_message="目标与来源是同一个会话，已跳过以避免回环",
                target=target,
            )
            return
        # 命中规则里的 QQ 屏蔽词：只跳过 QQ 目标，同一条规则的其他平台目标照常发。
        if (target.platform or Platform.QQ.value) == Platform.QQ.value:
            skip_reason = self._qq_skip_reason(rule, forwarded_text)
            if skip_reason:
                logger.info("Skip QQ target by rule %s: %s", rule.id, skip_reason)
                await self._safe_add_forward_log(
                    rule=rule,
                    message=message,
                    status=ForwardStatus.SKIPPED,
                    forwarded_text=forwarded_text,
                    error_message=skip_reason,
                    target=target,
                )
                return
        try:
            result = await self.router.deliver(rule, message, forwarded_text, target)
            # 审核降级时 sent_text 是实际发出的链接，note 说明降级原因。
            sent_text = getattr(result, "sent_text", None) or forwarded_text
            note = getattr(result, "note", None)
            await self._safe_add_forward_log(
                rule=rule,
                message=message,
                status=ForwardStatus.SUCCESS,
                forwarded_text=sent_text,
                error_message=note,
                target=target,
            )
            if note:
                logger.warning(
                    "Forward degraded by rule %s to %s: %s", rule.id, target.label, note
                )
            logger.info(
                "Forwarded %s message %s from account %s chat %s by rule %s to %s",
                message.source_platform,
                message.message_id,
                message.account_id,
                message.chat_id,
                rule.id,
                target.label,
            )
        except DeliveryPending as exc:
            # 已挂起等 token 刷新，不算失败；补发结果会另写一条日志。
            logger.info("Forward pending by rule %s to %s: %s", rule.id, target.label, exc)
            await self._safe_add_forward_log(
                rule=rule,
                message=message,
                status=ForwardStatus.PENDING,
                forwarded_text=forwarded_text,
                error_message=str(exc),
                target=target,
            )
        except Exception as exc:
            logger.exception("Failed to forward message by rule %s to %s", rule.id, target.label)
            error_message = str(exc).strip() or type(exc).__name__
            if isinstance(exc, TimeoutError):
                error_message = (
                    f"发送超时（QQ {self.settings.qq_send_timeout_seconds}s / "
                    f"微信 {self.settings.weixin_send_timeout_seconds}s / "
                    f"Telegram {self.settings.telegram_send_timeout_seconds}s）"
                )
            elif isinstance(exc, DeliveryError):
                error_message = str(exc)
            await self._safe_add_forward_log(
                rule=rule,
                message=message,
                status=ForwardStatus.FAILED,
                forwarded_text=forwarded_text,
                error_message=error_message,
                target=target,
            )

    @staticmethod
    def _qq_skip_reason(rule: ForwardRule, text: str) -> str | None:
        """规则里配了 QQ 屏蔽词且命中时返回原因，用于只跳过 QQ 目标。"""
        keywords = [item for item in (getattr(rule, "qq_skip_keywords", None) or []) if item]
        if not keywords:
            return None
        folded = (text or "").casefold()
        hits = [item for item in keywords if item.casefold() in folded]
        if not hits:
            return None
        return "命中 QQ 屏蔽词：" + "、".join(hits)

    @staticmethod
    def _is_echo_target(message: TelegramForwardMessage, target: ForwardTarget) -> bool:
        """目标就是来源会话本身 = 自己转给自己，跳过避免回环。"""
        platform = (target.platform or "").strip()
        if not platform or platform != message.source_platform:
            return False
        chat_id = id_key(message.chat_id)
        if chat_id is None:
            return False
        candidates = {
            (target.target_id or "").strip(),
            (target.delivery_id or "").strip(),
        }
        candidates.discard("")
        if not candidates:
            return False
        if platform == Platform.TELEGRAM.value and any(
            item.lower() in {"me", "self", "saved", "saved_messages"} for item in candidates
        ):
            return False
        return chat_id in candidates or chat_id.lstrip("-") in candidates
