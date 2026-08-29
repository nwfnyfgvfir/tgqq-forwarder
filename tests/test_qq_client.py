from __future__ import annotations

import asyncio

import botpy
from botpy import ConnectionSession

from app.qq_official.client import ForwarderQQClient, ensure_group_message_create_parser


def _dispatch_group_message(payload: dict) -> tuple[str, botpy.message.GroupMessage]:
    dispatched: list[tuple[str, botpy.message.GroupMessage]] = []
    ensure_group_message_create_parser()
    loop = asyncio.new_event_loop()
    try:
        connection = ConnectionSession(
            max_async=1,
            connect=lambda: None,
            dispatch=lambda event, message: dispatched.append((event, message)),
            loop=loop,
            api=None,
        )
        connection.parser["group_message_create"](payload)
    finally:
        loop.close()
    return dispatched[0]


def test_group_message_create_parser_dispatches_group_message() -> None:
    event_name, message = _dispatch_group_message(
        {
            "id": "event-1",
            "d": {
                "id": "msg-1",
                "content": "hello",
                "author": {"member_openid": "member-1"},
                "group_openid": "group-1",
                "mentions": [],
                "attachments": [],
            },
        }
    )

    assert event_name == "group_message_create"
    assert isinstance(message, botpy.message.GroupMessage)
    assert message.group_openid == "group-1"


async def test_on_group_message_create_caches_group_target() -> None:
    client = ForwarderQQClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    message = botpy.message.GroupMessage(
        None,
        "event-1",
        {
            "id": "msg-1",
            "content": "hello",
            "author": {"member_openid": "member-1"},
            "group_openid": "group-1",
            "mentions": [],
            "attachments": [],
        },
    )

    await client.on_group_message_create(message)

    targets = client.list_cached_targets()
    assert len(targets) == 1
    assert targets[0].target_type == "group"
    assert targets[0].target_id == "group-1"
    assert targets[0].last_message_id == "msg-1"
