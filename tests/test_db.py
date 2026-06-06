from __future__ import annotations

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
