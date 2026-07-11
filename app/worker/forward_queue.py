from __future__ import annotations

import asyncio
import logging
import time

from app.config import Settings
from app.qq_official.models import QQOutboundMessage
from app.qq_official.sender import QQOfficialSender
from app.rules.models import TelegramForwardMessage
from app.rules.service import ForwardRuleService
from app.storage.models import ForwardStatus

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

    @property
    def size(self) -> int:
        return self.queue.qsize()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="forward-queue")
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
        logger.info("Forward queue stopped")

    async def enqueue(self, message: TelegramForwardMessage) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.error(
                "Forward queue is full; dropping Telegram message account=%s message=%s",
                message.account_id,
                message.message_id,
            )

    async def _run(self) -> None:
        while not self._stopping.is_set():
            message = await self.queue.get()
            try:
                await self._process_message(message)
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
                await self.qq_sender.send(outbound)
                await self.service.add_forward_log(
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
                await self.service.add_forward_log(
                    rule=rule,
                    message=message,
                    status=ForwardStatus.FAILED,
                    forwarded_text=forwarded_text,
                    error_message=str(exc),
                )
