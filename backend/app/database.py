from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


# Lightweight idempotent migrations for SQLite dev environments.
# Replace with Alembic when we move to Postgres.
_DOCUMENT_NEW_COLUMNS = {
    "classification_confidence": "FLOAT",
    "classification_reasoning": "TEXT",
    "page_count": "INTEGER",
    "processing_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
    "processing_error": "TEXT",
    "processed_at": "DATETIME",
}


def _existing_columns(sync_conn, table: str) -> set[str]:
    inspector = inspect(sync_conn)
    if not inspector.has_table(table):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


async def _migrate(conn) -> None:
    existing = await conn.run_sync(_existing_columns, "documents")
    for col, sql_type in _DOCUMENT_NEW_COLUMNS.items():
        if col not in existing:
            await conn.execute(text(f"ALTER TABLE documents ADD COLUMN {col} {sql_type}"))


async def init_db() -> None:
    from . import models  # noqa: F401  ensure models are registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)
