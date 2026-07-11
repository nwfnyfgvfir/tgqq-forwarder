from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.rules.models import TelegramForwardMessage
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
    def __init__(self) -> None:
        self.matcher_calls = 0
        self.formatter = SimpleNamespace(format=lambda rule, message: message.text)

    async def is_paused(self) -> bool:
        return False

    async def matching_rules(self, message: TelegramForwardMessage):
        self.matcher_calls += 1
        return []


@pytest.mark.asyncio
async def test_cross_account_dedupe_skips_second_account() -> None:
    settings = Settings(telegram_dedupe_cross_account=True)
    service = FakeService()
    queue = ForwardQueue(settings, service, qq_sender=SimpleNamespace())

    first = make_message(account_id="main")
    second = make_message(account_id="news")
    await queue._process_message(first)
    await queue._process_message(second)

    assert service.matcher_calls == 1


@pytest.mark.asyncio
async def test_cross_account_dedupe_disabled_processes_both() -> None:
    settings = Settings(telegram_dedupe_cross_account=False)
    service = FakeService()
    queue = ForwardQueue(settings, service, qq_sender=SimpleNamespace())

    await queue._process_message(make_message(account_id="main"))
    await queue._process_message(make_message(account_id="news"))

    assert service.matcher_calls == 2
