"""Async SQLAlchemy + Postgres + pgvector wiring.

Single store: relational rows AND embedding vectors live in Postgres so
joins between scope items, citations, chunks, and embeddings are ACID
and run inside a single query — no second-store hop to Chroma.

Migrations are managed by Alembic (see backend/alembic/). The legacy
home-grown idempotent ALTER TABLE pattern lived in this file for SQLite
dev and is gone — Alembic owns schema evolution end-to-end.
"""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


# echo=False keeps the SQL log quiet in production. Flip via env if a
# query plan needs inspection.
engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Ensure the pgvector extension is present.

    Schema creation + migration is Alembic's job. We DO NOT create_all here
    anymore — that would fight Alembic's revision history. The lifespan
    hook calls this so a fresh deployment automatically gets the extension
    enabled before Alembic runs.
    """
    from . import models  # noqa: F401  ensure models register on Base.metadata

    if settings.database_url.startswith("postgresql"):
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
