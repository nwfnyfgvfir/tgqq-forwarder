from __future__ import annotations

import asyncio
import logging
import time

from app.config import Settings
from app.qq_official.models import QQOutboundMessage
from app.qq_official.sender import QQOfficialSender
from app.rules.models import TelegramForwardMessage
from app.rules.service import ForwardRuleService
from app.storage.models import ForwardRule, ForwardStatus

logger = logging.getLogger(__name__)


class ForwardQueue:
    def __init__(
        self,
        settings: Settings,
        service: ForwardRuleService,
        qq_sender: QQOfficialSender,
    ) -> None:
        self.settings = settings
        self.service = service
        self.qq_sender = qq_sender
        self.queue: asyncio.Queue[TelegramForwardMessage] = asyncio.Queue(
            maxsize=settings.forward_queue_size
        )
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._recent_message_keys: dict[tuple[int | None, int], float] = {}
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
    ) -> None:
        try:
            await self.service.add_forward_log(
                rule=rule,
                message=message,
                status=status,
                forwarded_text=forwarded_text,
                error_message=error_message,
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
            outbound = QQOutboundMessage(
                target_type=rule.qq_target_type,
                target_id=rule.qq_channel_id or rule.qq_target_id,
                text=forwarded_text,
                media_path=message.media_path,
                media_type=message.media_type,
                media_paths=message.media_paths,
                media_types=message.media_types,
                media_caption=rule.name.strip() or "媒体",
                guild_id=rule.qq_guild_id,
                channel_id=rule.qq_channel_id,
            )
            try:
                timeout = self.settings.qq_send_timeout_seconds
                if timeout and timeout > 0:
                    await asyncio.wait_for(
                        self.qq_sender.send(outbound),
                        timeout=timeout,
                    )
                else:
                    await self.qq_sender.send(outbound)
                await self._safe_add_forward_log(
                    rule=rule,
                    message=message,
                    status=ForwardStatus.SUCCESS,
                    forwarded_text=forwarded_text,
                )
                logger.info(
                    "Forwarded Telegram message %s from account %s chat %s by rule %s to QQ %s:%s",
                    message.message_id,
                    message.account_id,
                    message.chat_id,
                    rule.id,
                    rule.qq_target_type,
                    rule.qq_target_id,
                )
            except Exception as exc:
                logger.exception("Failed to forward Telegram message by rule %s", rule.id)
                error_message = str(exc).strip() or type(exc).__name__
                if isinstance(exc, TimeoutError):
                    error_message = (
                        f"QQ send timed out after {self.settings.qq_send_timeout_seconds}s"
                    )
                await self._safe_add_forward_log(
                    rule=rule,
                    message=message,
                    status=ForwardStatus.FAILED,
                    forwarded_text=forwarded_text,
                    error_message=error_message,
                )
