from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.channels.router import DeliveryError, DeliveryRouter
from app.config import Settings
from app.platforms import TELEGRAM_TARGET_BOT, TELEGRAM_TARGET_CHAT
from app.rules.models import TelegramForwardMessage
from app.storage.models import ForwardRule, ForwardStatus, ForwardTarget
from app.telegram_outbound.delivery import (
    TelegramBotSender,
    TelegramDelivery,
    TelegramDeliveryError,
    TelegramUserSender,
)
from app.telegram_outbound.registry import SentMessageRegistry
from app.worker.forward_queue import ForwardQueue


def make_settings(**overrides) -> Settings:
    data = {"_env_file": None, "telegram_send_timeout_seconds": 1.0}
    data.update(overrides)
    return Settings(**data)


def make_message(**overrides) -> TelegramForwardMessage:
    data = {
        "message_id": "qq-1",
        "chat_id": "group-1",
        "chat_title": "QQ群 1234",
        "chat_type": "group",
        "sender_id": "member-1",
        "sender_username": None,
        "sender_display_name": "member-1",
        "sender_is_bot": False,
        "text": "hello",
        "media_type": None,
        "media_path": None,
        "date": None,
        "source_platform": "qq",
    }
    data.update(overrides)
    return TelegramForwardMessage(**data)


def telegram_target(
    target_id: str,
    *,
    target_type: str = TELEGRAM_TARGET_CHAT,
    account_id: str | None = None,
) -> ForwardTarget:
    return ForwardTarget(
        rule_id=0,
        platform="telegram",
        target_type=target_type,
        target_id=target_id,
        account_id=account_id,
    )


class FakeSentMessage:
    def __init__(self, chat_id: object, message_id: object) -> None:
        self.chat_id = chat_id
        self.id = message_id


class FakeTelethonClient:
    def __init__(self) -> None:
        self.messages: list[tuple[object, str, dict]] = []
        self.files: list[tuple[object, str, dict]] = []

    async def send_message(self, entity, text, **kwargs):
        self.messages.append((entity, text, kwargs))
        return FakeSentMessage(entity if isinstance(entity, int) else -1001, 11)

    async def send_file(self, entity, path, **kwargs):
        self.files.append((entity, path, kwargs))
        return FakeSentMessage(entity if isinstance(entity, int) else -1001, 12)


class FakeListener:
    def __init__(self, client: FakeTelethonClient) -> None:
        self.client = client
        self.is_authorized = True
        self.account_id = "main"


class FakeAccountManager:
    def __init__(self, listener: FakeListener | None) -> None:
        self.listener = listener
        self.requested: list[str | None] = []

    def get(self, account_id: str | None = None):
        self.requested.append(account_id)
        return self.listener

    def is_any_connected(self) -> bool:
        return self.listener is not None


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.initialized = False

    async def send_message(self, **kwargs):
        self.calls.append(("message", kwargs))
        return SimpleNamespace(
            chat=SimpleNamespace(id=kwargs["chat_id"]), message_id=21
        )

    async def send_photo(self, **kwargs):
        self.calls.append(("photo", kwargs))
        return SimpleNamespace(
            chat=SimpleNamespace(id=kwargs["chat_id"]), message_id=22
        )

    async def send_document(self, **kwargs):
        self.calls.append(("document", kwargs))
        return SimpleNamespace(
            chat=SimpleNamespace(id=kwargs["chat_id"]), message_id=23
        )


def test_registry_remembers_and_expires() -> None:
    registry = SentMessageRegistry()
    registry.remember(-1001, 5)
    assert registry.is_recent(-1001, 5) is True
    assert registry.is_recent(-1002, 5) is False

    expired = SentMessageRegistry(ttl_seconds=-1.0)
    expired.remember(-1001, 5)
    assert expired.is_recent(-1001, 5) is False


def test_registry_ignores_missing_values() -> None:
    registry = SentMessageRegistry()
    registry.remember(None, 1)
    registry.remember(-1001, None)

    assert registry.is_recent(None, 1) is False
    assert registry.is_recent(-1001, None) is False


def test_registry_consumes_outgoing_text_once() -> None:
    registry = SentMessageRegistry()
    registry.mark_outgoing("hi")

    assert registry.consume_outgoing("hi") is True
    # 一条登记只吞一条消息，避免误伤用户随后手打的同样内容。
    assert registry.consume_outgoing("hi") is False


def test_registry_outgoing_text_expires() -> None:
    registry = SentMessageRegistry(outgoing_ttl_seconds=-1.0)
    registry.mark_outgoing("hi")

    assert registry.consume_outgoing("hi") is False


async def test_user_sender_sends_text_and_records_message() -> None:
    registry = SentMessageRegistry()
    client = FakeTelethonClient()
    manager = FakeAccountManager(FakeListener(client))
    sender = TelegramUserSender(make_settings(), lambda: manager, registry)

    await sender.send(telegram_target("-1001234", account_id="main"), make_message(), "hi")

    assert client.messages[0][0] == -1001234
    assert client.messages[0][1] == "hi"
    assert manager.requested == ["main"]
    assert registry.is_recent(-1001234, 11) is True
    # 发送前登记的正文用于兜住「更新先到」的竞态。
    assert registry.consume_outgoing("hi") is True


async def test_user_sender_resolves_saved_messages_alias() -> None:
    client = FakeTelethonClient()
    sender = TelegramUserSender(
        make_settings(), lambda: FakeAccountManager(FakeListener(client)), SentMessageRegistry()
    )

    await sender.send(telegram_target("me"), make_message(), "hi")

    assert client.messages[0][0] == "me"


async def test_user_sender_sends_media_with_caption(tmp_path: Path) -> None:
    first = tmp_path / "a.png"
    second = tmp_path / "b.pdf"
    first.write_bytes(b"x")
    second.write_bytes(b"y")
    client = FakeTelethonClient()
    sender = TelegramUserSender(
        make_settings(), lambda: FakeAccountManager(FakeListener(client)), SentMessageRegistry()
    )
    message = make_message(
        media_type="photo",
        media_path=first,
        media_paths=[first, second],
        media_types=["photo", "document"],
    )

    await sender.send(telegram_target("-1001"), message, "caption")

    assert [Path(item[1]).name for item in client.files] == ["a.png", "b.pdf"]
    assert client.files[0][2]["caption"] == "caption"
    assert client.messages == []


async def test_user_sender_requires_connected_account() -> None:
    sender = TelegramUserSender(
        make_settings(), lambda: FakeAccountManager(None), SentMessageRegistry()
    )

    with pytest.raises(TelegramDeliveryError):
        await sender.send(telegram_target("-1001"), make_message(), "hi")


async def test_bot_sender_sends_text_and_records_message() -> None:
    registry = SentMessageRegistry()
    bot = FakeBot()
    sender = TelegramBotSender(make_settings(), registry, bot=bot)

    await sender.send(
        telegram_target("@my_channel", target_type=TELEGRAM_TARGET_BOT),
        make_message(),
        "hi",
    )

    assert bot.initialized is True
    assert bot.calls[0][0] == "message"
    assert bot.calls[0][1]["chat_id"] == "@my_channel"
    assert registry.is_recent("@my_channel", 21) is True


async def test_bot_sender_rejects_saved_messages() -> None:
    sender = TelegramBotSender(make_settings(), SentMessageRegistry(), bot=FakeBot())

    with pytest.raises(TelegramDeliveryError):
        await sender.send(
            telegram_target("me", target_type=TELEGRAM_TARGET_BOT), make_message(), "hi"
        )


async def test_bot_sender_requires_token() -> None:
    sender = TelegramBotSender(make_settings(), SentMessageRegistry())

    assert sender.configured is False
    with pytest.raises(TelegramDeliveryError):
        await sender.send(
            telegram_target("-1001", target_type=TELEGRAM_TARGET_BOT), make_message(), "hi"
        )


async def test_delivery_dispatches_by_target_type() -> None:
    calls: list[str] = []

    class FakeUserSender:
        async def send(self, target, message, text):
            calls.append("user")

    class FakeBotSender:
        async def send(self, target, message, text):
            calls.append("bot")

    delivery = TelegramDelivery(
        make_settings(),
        lambda: None,
        user_sender=FakeUserSender(),  # type: ignore[arg-type]
        bot_sender=FakeBotSender(),  # type: ignore[arg-type]
    )

    await delivery.send(telegram_target("-1001"), make_message(), "hi")
    await delivery.send(
        telegram_target("-1001", target_type=TELEGRAM_TARGET_BOT), make_message(), "hi"
    )

    assert calls == ["user", "bot"]


async def test_router_delivers_to_telegram() -> None:
    sent: list[ForwardTarget] = []

    class FakeDelivery:
        enabled = True

        async def send(self, target, message, text):
            sent.append(target)

    router = DeliveryRouter(
        make_settings(),
        object(),  # type: ignore[arg-type]
        None,
        FakeDelivery(),  # type: ignore[arg-type]
    )
    rule = ForwardRule(name="r", qq_target_id="x", message_template="{text}")

    await router.deliver(rule, make_message(), "hi", telegram_target("-1001"))

    assert [item.target_id for item in sent] == ["-1001"]
    assert "telegram" in router.target_platforms


async def test_router_rejects_telegram_without_delivery() -> None:
    router = DeliveryRouter(make_settings(), object(), None)  # type: ignore[arg-type]
    rule = ForwardRule(name="r", qq_target_id="x", message_template="{text}")

    with pytest.raises(DeliveryError):
        await router.deliver(rule, make_message(), "hi", telegram_target("-1001"))


class _FakeRuleService:
    def __init__(self, rule: ForwardRule, targets: list[ForwardTarget]) -> None:
        self.rule = rule
        self.targets = targets
        self.log_calls: list[dict] = []
        self.formatter = SimpleNamespace(format=lambda rule, message: message.text)

    async def is_paused(self) -> bool:
        return False

    async def matching_rules(self, message):
        return [self.rule]

    async def targets_for(self, rule):
        return self.targets

    async def add_forward_log(self, **kwargs) -> None:
        self.log_calls.append(kwargs)


async def test_queue_skips_delivery_back_to_source_chat() -> None:
    sent: list[str] = []

    class FakeDelivery:
        enabled = True

        async def send(self, target, message, text):
            sent.append(target.target_id)

    settings = make_settings()
    router = DeliveryRouter(settings, object(), None, FakeDelivery())  # type: ignore[arg-type]
    rule = ForwardRule(name="r", qq_target_id="x", message_template="{text}")
    service = _FakeRuleService(rule, [telegram_target("-100123")])
    queue = ForwardQueue(settings, service, object(), None, router=router)  # type: ignore[arg-type]

    await queue._process_message(
        make_message(source_platform="telegram", chat_id=-100123)
    )

    assert sent == []
    assert service.log_calls[0]["status"] == ForwardStatus.SKIPPED


async def test_queue_skips_qq_echo_to_same_group() -> None:
    sent: list[str] = []

    class FakeDelivery:
        enabled = True

        async def send(self, target, message, text):
            sent.append(target.target_id)

    settings = make_settings()
    router = DeliveryRouter(settings, object(), None, FakeDelivery())  # type: ignore[arg-type]
    rule = ForwardRule(name="r", qq_target_id="x", message_template="{text}")
    qq_target = ForwardTarget(
        rule_id=0, platform="qq", target_type="group", target_id="group-1"
    )
    service = _FakeRuleService(rule, [qq_target])
    queue = ForwardQueue(settings, service, object(), None, router=router)  # type: ignore[arg-type]

    await queue._process_message(make_message(source_platform="qq", chat_id="group-1"))

    assert sent == []
    assert service.log_calls[0]["status"] == ForwardStatus.SKIPPED


async def test_queue_forwards_to_other_telegram_chat() -> None:
    sent: list[str] = []

    class FakeDelivery:
        enabled = True

        async def send(self, target, message, text):
            sent.append(target.target_id)

    settings = make_settings()
    router = DeliveryRouter(settings, object(), None, FakeDelivery())  # type: ignore[arg-type]
    rule = ForwardRule(name="r", qq_target_id="x", message_template="{text}")
    service = _FakeRuleService(rule, [telegram_target("-100999")])
    queue = ForwardQueue(settings, service, object(), None, router=router)  # type: ignore[arg-type]

    await queue._process_message(
        make_message(source_platform="telegram", chat_id=-100123)
    )

    assert sent == ["-100999"]
    assert service.log_calls[0]["status"] == ForwardStatus.SUCCESS
