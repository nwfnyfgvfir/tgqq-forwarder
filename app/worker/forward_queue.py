from __future__ import annotations

import asyncio
import logging

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
            logger.error("Forward queue is full; dropping Telegram message %s", message.message_id)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            message = await self.queue.get()
            try:
                await self._process_message(message)
            finally:
                self.queue.task_done()

    async def _process_message(self, message: TelegramForwardMessage) -> None:
        if await self.service.is_paused():
            logger.info("Forwarding is paused; skip Telegram message %s", message.message_id)
            return

        rules = await self.service.matching_rules(message)
        if not rules:
            logger.debug(
                "No forwarding rule matched Telegram message chat=%s message=%s sender=%s",
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
                    "Forwarded Telegram message %s from chat %s by rule %s to QQ %s:%s",
                    message.message_id,
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
