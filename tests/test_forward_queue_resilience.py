from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardStatus
from app.worker.forward_queue import ForwardQueue


def make_message(**kwargs) -> TelegramForwardMessage:
    data = {
        "message_id": 11,
        "chat_id": -100123,
        "chat_title": "news",
        "chat_type": "channel",
        "sender_id": 42,
        "sender_username": "sender",
        "sender_display_name": "Sender",
        "sender_is_bot": False,
        "text": "hello",
        "media_type": None,
        "media_path": None,
        "date": None,
        "account_id": "main",
    }
    data.update(kwargs)
    return TelegramForwardMessage(**data)


class FakeService:
    def __init__(
        self,
        *,
        rules: list | None = None,
        raise_on_match: bool = False,
        raise_on_log: bool = False,
    ) -> None:
        self.rules = rules or []
        self.raise_on_match = raise_on_match
        self.raise_on_log = raise_on_log
        self.matcher_calls = 0
        self.log_calls: list[dict] = []
        self.formatter = SimpleNamespace(format=lambda rule, message: message.text)

    async def is_paused(self) -> bool:
        return False

    async def matching_rules(self, message: TelegramForwardMessage):
        self.matcher_calls += 1
        if self.raise_on_match:
            raise RuntimeError("match boom")
        return self.rules

    async def add_forward_log(self, **kwargs):
        if self.raise_on_log:
            raise RuntimeError("log boom")
        self.log_calls.append(kwargs)


class FakeSender:
    def __init__(self, *, hang: bool = False, fail: bool = False) -> None:
        self.hang = hang
        self.fail = fail
        self.calls = 0

    async def send(self, outbound) -> dict:
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("send boom")
        return {"ok": True}


def make_rule(rule_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=rule_id,
        name="rule",
        qq_target_type="group",
        qq_target_id="group-openid",
        qq_channel_id=None,
        qq_guild_id=None,
    )


@pytest.mark.asyncio
async def test_process_message_exception_does_not_kill_worker() -> None:
    settings = Settings(forward_queue_size=10)
    service = FakeService(raise_on_match=True)
    queue = ForwardQueue(settings, service, FakeSender())
    await queue.start()
    try:
        await queue.enqueue(make_message(message_id=1))
        await queue.enqueue(make_message(message_id=2))
        for _ in range(50):
            if service.matcher_calls >= 2 and queue.size == 0:
                break
            await asyncio.sleep(0.02)
        assert service.matcher_calls >= 2
        assert queue.alive is True
        assert queue.size == 0
        assert queue._error_total >= 1
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_failed_log_write_does_not_kill_worker() -> None:
    settings = Settings(forward_queue_size=10)
    service = FakeService(rules=[make_rule()], raise_on_log=True)
    sender = FakeSender(fail=True)
    queue = ForwardQueue(settings, service, sender)
    await queue.start()
    try:
        await queue.enqueue(make_message(message_id=1))
        await queue.enqueue(make_message(message_id=2))
        for _ in range(50):
            if sender.calls >= 2 and queue.size == 0:
                break
            await asyncio.sleep(0.02)
        assert sender.calls >= 2
        assert queue.alive is True
        assert queue.size == 0
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_send_timeout_marks_failed_and_continues() -> None:
    settings = Settings(forward_queue_size=10, qq_send_timeout_seconds=0.05)
    service = FakeService(rules=[make_rule()])
    sender = FakeSender(hang=True)
    queue = ForwardQueue(settings, service, sender)

    await queue._process_message(make_message(message_id=1))
    assert sender.calls == 1
    assert len(service.log_calls) == 1
    assert service.log_calls[0]["status"] == ForwardStatus.FAILED
    assert "timed out" in (service.log_calls[0]["error_message"] or "").lower()

    # Second message must still process even after timeout path.
    sender.hang = False
    await queue._process_message(make_message(message_id=2))
    assert sender.calls == 2
    assert service.log_calls[-1]["status"] == ForwardStatus.SUCCESS


@pytest.mark.asyncio
async def test_enqueue_drops_when_full() -> None:
    settings = Settings(forward_queue_size=1)
    service = FakeService()
    queue = ForwardQueue(settings, service, FakeSender())

    await queue.enqueue(make_message(message_id=1))
    await queue.enqueue(make_message(message_id=2))

    assert queue.size == 1
    assert queue.dropped_total == 1
    snapshot = queue.status_snapshot()
    assert snapshot["queue_size"] == 1
    assert snapshot["queue_max_size"] == 1
    assert snapshot["queue_dropped_total"] == 1
    assert snapshot["forward_consumer_alive"] is False


@pytest.mark.asyncio
async def test_supervisor_restarts_after_consumer_crash() -> None:
    settings = Settings(forward_queue_size=10)
    service = FakeService()
    queue = ForwardQueue(settings, service, FakeSender())

    original_run = queue._run

    async def boom_then_original() -> None:
        if queue._restart_total == 0 and not getattr(queue, "_boom_done", False):
            queue._boom_done = True  # type: ignore[attr-defined]
            raise RuntimeError("forced consumer crash")
        await original_run()

    queue._run = boom_then_original  # type: ignore[method-assign]
    await queue.start()
    try:
        for _ in range(100):
            if queue.restart_total >= 1 and queue.alive:
                break
            await asyncio.sleep(0.02)
        assert queue.restart_total >= 1
        assert queue.alive is True
        await queue.enqueue(make_message(message_id=99))
        for _ in range(50):
            if service.matcher_calls >= 1 and queue.size == 0:
                break
            await asyncio.sleep(0.02)
        assert service.matcher_calls >= 1
    finally:
        await queue.stop()
