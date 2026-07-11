from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, echo=False, future=True)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            await conn.run_sync(_ensure_schema_compat)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()


def _ensure_schema_compat(sync_conn) -> None:
    """Add newly introduced nullable columns for existing SQLite databases."""
    dialect = sync_conn.dialect.name
    if dialect != "sqlite":
        return

    _ensure_column(sync_conn, "forwardrule", "source_account_id", "VARCHAR(64)")
    _ensure_column(sync_conn, "forwardlog", "tg_account_id", "VARCHAR(64)")
    _ensure_column(sync_conn, "forwardlog", "tg_account_user_id", "INTEGER")


def _ensure_column(sync_conn, table: str, column: str, column_type: str) -> None:
    rows = sync_conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    existing = {row[1] for row in rows}
    if column in existing:
        return
    sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
