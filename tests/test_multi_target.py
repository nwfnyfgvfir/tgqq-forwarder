from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.channels.router import DeliveryRouter
from app.config import Settings
from app.rules.models import TelegramForwardMessage
from app.rules.service import ForwardRuleService
from app.storage.db import Database
from app.storage.models import ForwardRule, ForwardStatus, ForwardTarget
from app.web.schemas import RuleCreateRequest, RuleResponse
from app.worker.forward_queue import ForwardQueue


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
        "text": "hello",
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
        "qq_target_type": "group",
        "qq_target_id": "qq-a",
        "message_template": "{text}",
    }
    data.update(overrides)
    return ForwardRule(**data)


async def open_service(tmp_path) -> tuple[Database, ForwardRuleService]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await db.init()
    return db, ForwardRuleService(db)


def target(
    platform: str, target_type: str, target_id: str, *, enabled: bool = True
) -> ForwardTarget:
    return ForwardTarget(
        rule_id=0,
        platform=platform,
        target_type=target_type,
        target_id=target_id,
        enabled=enabled,
    )


def test_schema_targets_drive_legacy_fields() -> None:
    payload = RuleCreateRequest(
        name="multi",
        message_template="{text}",
        targets=[
            {"platform": "qq", "target_type": "group", "target_id": "qq-a"},
            {"platform": "weixin", "target_type": "group", "target_id": "wxid_b"},
        ],
    )

    values = payload.to_rule_values()
    assert values["target_platform"] == "qq"
    assert values["qq_target_type"] == "group"
    assert values["qq_target_id"] == "qq-a"

    targets = payload.to_targets()
    assert [item.target_id for item in targets] == ["qq-a", "wxid_b"]
    # 微信目标类型强制 friend，频道字段被清空。
    assert targets[1].target_type == "friend"
    assert targets[1].channel_id is None


def test_schema_requires_a_target() -> None:
    with pytest.raises(ValidationError):
        RuleCreateRequest(name="x", message_template="{text}")


def test_schema_empty_targets_fall_back_to_legacy_fields() -> None:
    payload = RuleCreateRequest(
        name="x",
        qq_target_id="qq-a",
        message_template="{text}",
        targets=[],
    )
    assert payload.targets is None
    assert [item.target_id for item in payload.to_targets()] == ["qq-a"]


async def test_service_persists_every_target(tmp_path) -> None:
    db, service = await open_service(tmp_path)
    try:
        result = await service.create_or_merge_rule(
            make_rule(),
            [
                target("qq", "group", "qq-a"),
                target("qq", "c2c", "qq-b"),
                target("weixin", "friend", "wxid_c"),
            ],
        )
        rule = result.rule
        rows = await service.target_rows(int(rule.id or 0))
        assert [row.target_id for row in rows] == ["qq-a", "qq-b", "wxid_c"]
        # 兼容列跟随第一个目标，旧代码/旧接口行为不变。
        assert rule.target_platform == "qq"
        assert rule.qq_target_type == "group"
        assert rule.qq_target_id == "qq-a"

        response = RuleResponse.from_rule(rule, rows)
        assert [item.target_id for item in response.targets] == ["qq-a", "qq-b", "wxid_c"]
    finally:
        await db.dispose()


async def test_service_update_replaces_targets(tmp_path) -> None:
    db, service = await open_service(tmp_path)
    try:
        created = await service.create_or_merge_rule(
            make_rule(), [target("qq", "group", "qq-a"), target("qq", "group", "qq-b")]
        )
        rule_id = int(created.rule.id or 0)
        updated = await service.update_rule(
            rule_id,
            {"name": "rule"},
            [target("weixin", "friend", "wxid_z")],
        )
        assert updated is not None
        rows = await service.target_rows(rule_id)
        assert [row.target_id for row in rows] == ["wxid_z"]
        assert updated.target_platform == "weixin"
        assert updated.qq_target_type == "friend"
        assert updated.qq_target_id == "wxid_z"
    finally:
        await db.dispose()


async def test_targets_for_falls_back_to_legacy_columns(tmp_path) -> None:
    db, service = await open_service(tmp_path)
    try:
        legacy = await service.create_rule(make_rule(qq_target_id="legacy-openid"))
        rows = await service.targets_for(legacy)
        assert len(rows) == 1
        assert rows[0].platform == "qq"
        assert rows[0].target_id == "legacy-openid"
    finally:
        await db.dispose()


async def test_targets_for_skips_disabled_targets(tmp_path) -> None:
    db, service = await open_service(tmp_path)
    try:
        created = await service.create_or_merge_rule(
            make_rule(),
            [
                target("qq", "group", "qq-a", enabled=False),
                target("qq", "group", "qq-b"),
            ],
        )
        rows = await service.targets_for(created.rule)
        assert [row.target_id for row in rows] == ["qq-b"]

        rule_id = int(created.rule.id or 0)
        await service.update_rule(
            rule_id,
            {"name": "rule"},
            [target("qq", "group", "qq-a", enabled=False)],
        )
        refreshed = await service.get_rule(rule_id)
        assert refreshed is not None
        # 全部停用时不再回退到兼容列，避免「停用」被静默绕过。
        assert await service.targets_for(refreshed) == []
    finally:
        await db.dispose()


async def test_duplicate_rule_copies_targets(tmp_path) -> None:
    db, service = await open_service(tmp_path)
    try:
        created = await service.create_or_merge_rule(
            make_rule(), [target("qq", "group", "qq-a"), target("weixin", "friend", "wxid_c")]
        )
        copy = await service.duplicate_rule(int(created.rule.id or 0), name="copy")
        assert copy is not None
        rows = await service.target_rows(int(copy.id or 0))
        assert [row.target_id for row in rows] == ["qq-a", "wxid_c"]
    finally:
        await db.dispose()


async def test_merge_keeps_rules_with_different_targets_separate(tmp_path) -> None:
    db, service = await open_service(tmp_path)
    try:
        first = await service.create_or_merge_rule(make_rule(), [target("qq", "group", "qq-a")])
        second = await service.create_or_merge_rule(make_rule(), [target("qq", "group", "qq-b")])
        assert first.created
        assert second.created
        assert len(await service.list_rules()) == 2
    finally:
        await db.dispose()


class FakeQQSender:
    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.calls: list = []
        self.fail_ids = fail_ids or set()

    async def send(self, outbound) -> dict:
        self.calls.append(outbound)
        if outbound.target_id in self.fail_ids:
            raise RuntimeError("qq boom")
        return {"ok": True}


class FakeWeixinService:
    def __init__(self) -> None:
        self.enabled = True
        self.calls: list = []

    async def send(self, outbound) -> None:
        self.calls.append(outbound)


class FakeService:
    """只实现 ForwardQueue 需要的接口，targets_for 直接返回预置目标。"""

    def __init__(self, rule, targets) -> None:
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

    async def add_forward_log(self, **kwargs):
        self.log_calls.append(kwargs)


def make_router(settings: Settings, qq: FakeQQSender, weixin: FakeWeixinService):
    return DeliveryRouter(settings, qq, weixin)  # type: ignore[arg-type]


async def test_forward_queue_delivers_to_every_target() -> None:
    settings = Settings()
    qq = FakeQQSender()
    weixin = FakeWeixinService()
    rule = SimpleNamespace(id=1, name="rule")
    service = FakeService(
        rule,
        [
            target("qq", "group", "qq-a"),
            target("qq", "c2c", "qq-b"),
            target("weixin", "friend", "wxid_c"),
        ],
    )
    queue = ForwardQueue(settings, service, qq, weixin, router=make_router(settings, qq, weixin))

    await queue._process_message(make_message())

    assert [item.target_id for item in qq.calls] == ["qq-a", "qq-b"]
    assert [item.target_user_id for item in weixin.calls] == ["wxid_c"]
    assert len(service.log_calls) == 3
    assert all(call["status"] == ForwardStatus.SUCCESS for call in service.log_calls)
    assert [call["target"].target_id for call in service.log_calls] == ["qq-a", "qq-b", "wxid_c"]


async def test_one_target_failure_does_not_block_others() -> None:
    settings = Settings()
    qq = FakeQQSender(fail_ids={"qq-a"})
    weixin = FakeWeixinService()
    rule = SimpleNamespace(id=1, name="rule")
    service = FakeService(
        rule,
        [target("qq", "group", "qq-a"), target("weixin", "friend", "wxid_c")],
    )
    queue = ForwardQueue(settings, service, qq, weixin, router=make_router(settings, qq, weixin))

    await queue._process_message(make_message())

    assert [item.target_user_id for item in weixin.calls] == ["wxid_c"]
    assert [call["status"] for call in service.log_calls] == [
        ForwardStatus.FAILED,
        ForwardStatus.SUCCESS,
    ]
    assert service.log_calls[0]["target"].target_id == "qq-a"
    assert "qq boom" in service.log_calls[0]["error_message"]


async def test_rule_without_enabled_target_sends_nothing() -> None:
    settings = Settings()
    qq = FakeQQSender()
    weixin = FakeWeixinService()
    service = FakeService(SimpleNamespace(id=1, name="rule"), [])
    queue = ForwardQueue(settings, service, qq, weixin, router=make_router(settings, qq, weixin))

    await queue._process_message(make_message())

    assert qq.calls == []
    assert service.log_calls == []


async def test_router_prefers_explicit_target() -> None:
    settings = Settings()
    qq = FakeQQSender()
    weixin = FakeWeixinService()
    router = make_router(settings, qq, weixin)
    rule = make_rule(qq_target_id="legacy-openid")

    await router.deliver(rule, make_message(), "hi", target("weixin", "friend", "wxid_x"))

    assert qq.calls == []
    assert [item.target_user_id for item in weixin.calls] == ["wxid_x"]


async def test_router_channel_target_uses_channel_id() -> None:
    settings = Settings()
    qq = FakeQQSender()
    weixin = FakeWeixinService()
    router = make_router(settings, qq, weixin)
    rule = make_rule()

    await router.deliver(
        rule,
        make_message(),
        "hi",
        ForwardTarget(
            rule_id=0,
            platform="qq",
            target_type="channel",
            target_id="channel-a",
            guild_id="guild-a",
            channel_id="channel-b",
        ),
    )

    assert qq.calls[0].target_id == "channel-b"
    assert qq.calls[0].guild_id == "guild-a"


async def test_legacy_database_gains_target_table_without_data_loss(tmp_path) -> None:
    from sqlalchemy import text

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await db.init()
    async with db.engine.begin() as conn:
        # 模拟升级前的老库：没有 forward_target 表，规则目标只存在历史列里。
        await conn.execute(text("DROP TABLE forward_target"))
        await conn.execute(
            text(
                "INSERT INTO forwardrule (name, enabled, qq_target_type, qq_target_id,"
                " message_template, priority, target_platform, created_at, updated_at)"
                " VALUES ('legacy', 1, 'group', 'legacy-openid', '{text}', 0, 'qq',"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
    await db.init()
    service = ForwardRuleService(db)
    try:
        rules = await service.list_rules()
        assert len(rules) == 1
        assert await service.target_rows(int(rules[0].id or 0)) == []
        rows = await service.targets_for(rules[0])
        assert [(row.platform, row.target_id) for row in rows] == [("qq", "legacy-openid")]
    finally:
        await db.dispose()
