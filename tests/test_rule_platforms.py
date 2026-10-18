from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.rules.matcher import RuleMatcher
from app.rules.models import TelegramForwardMessage
from app.storage.db import Database
from app.storage.models import ForwardRule
from app.web.schemas import RuleCreateRequest, RuleResponse


def make_message(**overrides) -> TelegramForwardMessage:
    data = {
        "message_id": 1,
        "chat_id": -100123,
        "chat_title": "news",
        "chat_type": "channel",
        "sender_id": 42,
        "sender_username": "sender",
        "sender_display_name": "Sender",
        "sender_is_bot": False,
        "text": "hello world",
        "media_type": None,
        "media_path": None,
        "date": None,
        "account_id": "main",
    }
    data.update(overrides)
    return TelegramForwardMessage(**data)


def make_rule(**overrides) -> ForwardRule:
    data = {
        "name": "rule",
        "enabled": True,
        "qq_target_type": "group",
        "qq_target_id": "target",
        "message_template": "{text}",
    }
    data.update(overrides)
    return ForwardRule(**data)


def test_source_platform_none_matches_all_platforms() -> None:
    matcher = RuleMatcher()
    rule = make_rule()
    assert matcher.matches(rule, make_message()) is True
    assert matcher.matches(rule, make_message(source_platform="weixin", chat_id="wxid_1")) is True


def test_source_platform_filters_messages() -> None:
    matcher = RuleMatcher()
    rule = make_rule(source_platform="weixin")
    assert matcher.matches(rule, make_message(source_platform="weixin", chat_id="wxid_1")) is True
    assert matcher.matches(rule, make_message(source_platform="telegram")) is False


def test_weixin_chat_id_is_matched_as_string() -> None:
    matcher = RuleMatcher()
    rule = make_rule(source_platform="weixin", source_chat_id="wxid_abc")
    assert matcher.matches(rule, make_message(source_platform="weixin", chat_id="wxid_abc"))
    assert not matcher.matches(rule, make_message(source_platform="weixin", chat_id="wxid_other"))


def test_numeric_chat_id_still_matches_telegram_messages() -> None:
    matcher = RuleMatcher()
    rule = make_rule(source_chat_id=-100123)
    assert matcher.matches(rule, make_message(chat_id=-100123)) is True
    assert matcher.matches(rule, make_message(chat_id=-100999)) is False


def test_schema_weixin_target_forces_friend_type() -> None:
    payload = RuleCreateRequest(
        name="wx",
        target_platform="weixin",
        qq_target_type="group",
        qq_target_id="wxid_1",
        qq_guild_id="guild",
        qq_channel_id="channel",
        message_template="{text}",
    )
    values = payload.to_rule_values()
    assert values["target_platform"] == "weixin"
    assert values["qq_target_type"] == "friend"
    assert values["qq_guild_id"] is None
    assert values["qq_channel_id"] is None


def test_schema_rejects_unknown_platforms() -> None:
    with pytest.raises(ValidationError):
        RuleCreateRequest(
            name="x",
            target_platform="telegram",
            qq_target_id="t",
            message_template="{text}",
        )
    with pytest.raises(ValidationError):
        RuleCreateRequest(
            name="x",
            source_platform="qq",
            qq_target_id="t",
            message_template="{text}",
        )


def test_schema_rejects_group_chat_type_for_weixin_source() -> None:
    with pytest.raises(ValidationError):
        RuleCreateRequest(
            name="x",
            source_platform="weixin",
            source_chat_type="group",
            qq_target_id="t",
            message_template="{text}",
        )


def test_schema_accepts_wxid_source_chat_id() -> None:
    payload = RuleCreateRequest(
        name="x",
        source_platform="weixin",
        source_chat_id="wxid_abc",
        source_chat_type="private",
        qq_target_id="t",
        message_template="{text}",
    )
    assert payload.to_rule_values()["source_chat_id"] == "wxid_abc"


async def test_rule_roundtrip_keeps_string_ids(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await db.init()
    async with db.session() as session:
        session.add(
            ForwardRule(
                name="weixin rule",
                source_platform="weixin",
                source_chat_id="wxid_abc",
                source_sender_id="wxid_abc",
                target_platform="weixin",
                qq_target_type="friend",
                qq_target_id="wxid_abc",
                message_template="{text}",
            )
        )
        session.add(
            ForwardRule(
                name="tg rule",
                source_chat_id=-100123,
                qq_target_id="openid",
                message_template="{text}",
            )
        )
    async with db.session() as session:
        from sqlalchemy import select

        result = await session.execute(select(ForwardRule))
        rows = result.scalars().all()
    await db.dispose()

    by_name = {row.name: row for row in rows}
    assert by_name["weixin rule"].source_chat_id == "wxid_abc"
    assert by_name["weixin rule"].target_platform == "weixin"
    assert by_name["tg rule"].source_chat_id == -100123
    assert by_name["tg rule"].target_platform == "qq"


async def test_rule_response_exposes_platforms(tmp_path) -> None:
    rule = ForwardRule(
        id=7,
        name="wx",
        source_platform="weixin",
        source_chat_id="wxid_abc",
        target_platform="weixin",
        qq_target_type="friend",
        qq_target_id="wxid_abc",
        message_template="{text}",
    )
    response = RuleResponse.from_rule(rule)
    assert response.source_platform == "weixin"
    assert response.target_platform == "weixin"
    assert response.source_chat_id == "wxid_abc"
