from __future__ import annotations

from app.rules.keywords import keywords_from_text_include_regex, keywords_to_text_include_regex
from app.rules.service import ForwardRuleService
from app.storage.db import Database
from app.storage.models import ForwardRule
from app.storage.repositories import RuleRepository


async def test_database_create_and_list_rules(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await db.init()
    repo = RuleRepository()
    async with db.session() as session:
        await repo.create_rule(
            session,
            ForwardRule(
                name="r1",
                qq_target_type="group",
                qq_target_id="target",
                message_template="{text}",
            ),
        )
    async with db.session() as session:
        rules = await repo.list_rules(session)
    await db.dispose()
    assert len(rules) == 1
    assert rules[0].name == "r1"


async def test_create_or_merge_rule_merges_duplicate_keyword_rules(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await db.init()
    service = ForwardRuleService(db)

    first = await service.create_or_merge_rule(
        ForwardRule(
            name="LINUX DO Channel",
            source_chat_id=-1002035446470,
            qq_target_type="c2c",
            qq_target_id="target",
            text_include_regex=keywords_to_text_include_regex(["gpt", "注册机"]),
            message_template="{text}",
        )
    )
    second = await service.create_or_merge_rule(
        ForwardRule(
            name="LINUX DO Channel",
            source_chat_id=-1002035446470,
            qq_target_type="c2c",
            qq_target_id="target",
            text_include_regex=keywords_to_text_include_regex(["公益", "gpt"]),
            message_template="{text}",
        )
    )

    rules = await service.list_rules()
    await db.dispose()
    assert first.created
    assert not second.created
    assert second.updated
    assert len(rules) == 1
    assert keywords_from_text_include_regex(rules[0].text_include_regex) == [
        "gpt",
        "注册机",
        "公益",
    ]


async def test_create_or_merge_rule_removes_existing_duplicate_rows(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    await db.init()
    repo = RuleRepository()
    async with db.session() as session:
        await repo.create_rule(
            session,
            ForwardRule(
                name="r1",
                source_chat_id=-100,
                qq_target_type="group",
                qq_target_id="target",
                text_include_regex=keywords_to_text_include_regex(["AI"]),
                message_template="{text}",
            ),
        )
        await repo.create_rule(
            session,
            ForwardRule(
                name="r1",
                source_chat_id=-100,
                qq_target_type="group",
                qq_target_id="target",
                text_include_regex=keywords_to_text_include_regex(["Python"]),
                message_template="{text}",
            ),
        )

    service = ForwardRuleService(db)
    result = await service.create_or_merge_rule(
        ForwardRule(
            name="r1",
            source_chat_id=-100,
            qq_target_type="group",
            qq_target_id="target",
            text_include_regex=keywords_to_text_include_regex(["AI", "机器人"]),
            message_template="{text}",
        )
    )

    rules = await service.list_rules()
    await db.dispose()
    assert not result.created
    assert result.removed_duplicate_count == 1
    assert len(rules) == 1
    assert keywords_from_text_include_regex(rules[0].text_include_regex) == [
        "AI",
        "Python",
        "机器人",
    ]
