from __future__ import annotations

import botpy

from app.platforms import Platform
from app.qq_official.client import ForwarderQQClient


def make_client(captured: list) -> ForwarderQQClient:
    async def on_message(message) -> None:
        captured.append(message)

    return ForwarderQQClient(
        intents=botpy.Intents(public_messages=True),
        bot_log=False,
        on_message=on_message,
    )


def group_message(**overrides) -> botpy.message.GroupMessage:
    data = {
        "id": "msg-1",
        "content": "hello",
        "author": {"member_openid": "member-1"},
        "group_openid": "group-1",
        "mentions": [],
        "attachments": [],
        "timestamp": "2026-10-18T12:00:00+08:00",
    }
    data.update(overrides)
    return botpy.message.GroupMessage(None, "event-1", data)


async def test_group_message_becomes_forward_message() -> None:
    captured: list = []
    client = make_client(captured)

    await client.on_group_message_create(group_message())

    assert len(captured) == 1
    message = captured[0]
    assert message.source_platform == Platform.QQ.value
    assert message.chat_id == "group-1"
    assert message.chat_type == "group"
    assert message.sender_id == "member-1"
    assert message.text == "hello"
    assert message.sender_is_bot is False
    assert message.date is not None
    # 目标缓存行为不变
    assert client.list_cached_targets()[0].target_id == "group-1"


async def test_duplicate_event_is_emitted_once() -> None:
    captured: list = []
    client = make_client(captured)

    # 同一条消息可能同时触发 @ 事件与普通群消息事件。
    await client.on_group_message_create(group_message())
    await client.on_group_at_message_create(group_message())

    assert len(captured) == 1


async def test_mention_placeholder_is_stripped() -> None:
    captured: list = []
    client = make_client(captured)

    await client.on_group_message_create(group_message(content="<@!123456>  转发我"))

    assert captured[0].text == "转发我"


async def test_attachments_become_text_notes() -> None:
    captured: list = []
    client = make_client(captured)

    await client.on_group_message_create(
        group_message(
            content="看图",
            attachments=[
                {"content_type": "image/png", "url": "https://cdn.example/a.png"},
                {"content_type": "file", "url": "https://cdn.example/b.pdf", "filename": "b.pdf"},
            ],
        )
    )

    message = captured[0]
    assert message.media_type == "photo"
    assert "[图片] https://cdn.example/a.png" in message.text
    assert "[文件] b.pdf https://cdn.example/b.pdf" in message.text
    assert [link.url for link in message.links] == [
        "https://cdn.example/a.png",
        "https://cdn.example/b.pdf",
    ]


async def test_empty_message_is_skipped() -> None:
    captured: list = []
    client = make_client(captured)

    await client.on_group_message_create(group_message(content="   "))

    assert captured == []


async def test_c2c_message_uses_user_openid() -> None:
    captured: list = []
    client = make_client(captured)
    message = botpy.message.C2CMessage(
        None,
        "event-2",
        {
            "id": "c2c-1",
            "content": "私聊",
            "author": {"user_openid": "user-1"},
            "attachments": [],
        },
    )

    await client.on_c2c_message_create(message)

    assert captured[0].chat_id == "user-1"
    assert captured[0].chat_type == "private"
    assert captured[0].sender_id == "user-1"


async def test_channel_message_uses_channel_id() -> None:
    captured: list = []
    client = make_client(captured)
    message = botpy.message.Message(
        None,
        "event-3",
        {
            "id": "ch-1",
            "content": "频道消息",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "author": {"id": "42", "username": "alice"},
            "attachments": [],
        },
    )

    await client.on_at_message_create(message)

    assert captured[0].chat_id == "channel-1"
    assert captured[0].chat_type == "channel"
    assert captured[0].sender_id == "42"


async def test_client_without_callback_only_caches_targets() -> None:
    client = ForwarderQQClient(
        intents=botpy.Intents(public_messages=True),
        bot_log=False,
    )

    await client.on_group_message_create(group_message())

    assert client.list_cached_targets()[0].last_message_id == "msg-1"
