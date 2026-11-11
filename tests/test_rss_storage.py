from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, inspect
from sqlmodel import Session, select

from app.rss.schemas import DEFAULT_MESSAGE_TEMPLATE
from app.storage.models import RssEntry, RssSubscription, SQLModel


async def test_legacy_subscription_gets_local_detection_mode(tmp_path):
    from sqlalchemy import text

    from app.storage.db import Database

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE rss_subscription (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO rss_subscription VALUES (1)")
    db = Database(f"sqlite+aiosqlite:///{path}")
    try:
        await db.init()
        async with db.session() as session:
            mode = (await session.execute(text(
                "SELECT translation_detection FROM rss_subscription WHERE id = 1"
            ))).scalar_one()
            assert mode == "local"
            await session.execute(text(
                "UPDATE rss_subscription SET translation_detection = 'ai' WHERE id = 1"
            ))
        await db.init()
        async with db.session() as session:
            assert (await session.execute(text(
                "SELECT translation_detection FROM rss_subscription WHERE id = 1"
            ))).scalar_one() == "ai"
    finally:
        await db.dispose()


def test_rss_metadata_creates_new_tables_and_utc_roundtrip(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'rss.db'}")
    try:
        SQLModel.metadata.create_all(engine)
        SQLModel.metadata.create_all(engine)
        assert {"rss_subscription", "rss_entry"} <= set(inspect(engine).get_table_names())
        with Session(engine) as session:
            sub = RssSubscription(
                name="RSS",
                feed_url="https://example.com/feed",
                tg_chat_id="-100123",
                message_template=DEFAULT_MESSAGE_TEMPLATE,
            )
            session.add(sub)
            session.flush()
            sub_id = sub.id
            session.add(
                RssEntry(
                    subscription_id=sub_id,
                    entry_key="key",
                    title="Entry",
                    link="https://example.com/1",
                    summary="Text",
                    published_at=datetime(2026, 1, 1, 8, tzinfo=UTC) + timedelta(hours=1),
                )
            )
            session.commit()
        with Session(engine) as session:
            sub = session.get(RssSubscription, sub_id)
            row = session.exec(select(RssEntry)).one()
            assert sub.created_at.tzinfo == UTC and sub.updated_at.tzinfo == UTC
            assert row.created_at.tzinfo == UTC and row.published_at.tzinfo == UTC
            assert row.status == "pending" and row.attempts == 0
            assert row.tg_chat_id is None
        constraints = inspect(engine).get_unique_constraints("rss_entry")
        assert any(set(c["column_names"]) == {"subscription_id", "entry_key"} for c in constraints)
    finally:
        engine.dispose()
