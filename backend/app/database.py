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
_NEW_COLUMNS_BY_TABLE: dict[str, dict[str, str]] = {
    "documents": {
        "classification_confidence": "FLOAT",
        "classification_reasoning": "TEXT",
        "page_count": "INTEGER",
        "processing_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "processing_error": "TEXT",
        "processed_at": "DATETIME",
        # Two-stage workflow (project setup vs bid submissions)
        "source": "VARCHAR(32) NOT NULL DEFAULT 'project_document'",
        "vendor_name": "VARCHAR(255)",
        # Phase 11: vendor canonicalization
        "canonical_vendor": "VARCHAR(255)",
        "vendor_provenance": "JSON",
    },
    "projects": {
        "lifecycle_state": "VARCHAR(32) NOT NULL DEFAULT 'setup'",
    },
    "document_pages": {
        # Per-page text content for the chunker — populated by PyMuPDF or OCR
        "text_content": "TEXT",
        "text_source": "VARCHAR(16)",
    },
    "scope_items": {
        # Phase 6 quantity resolver
        "qty_confidence": "VARCHAR(16)",
        "qty_provenance": "JSON",
        # Phase 7 Opus reflection pass
        "verifier_status": "VARCHAR(16)",
        "verifier_review": "JSON",
    },
}


def _existing_columns(sync_conn, table: str) -> set[str]:
    inspector = inspect(sync_conn)
    if not inspector.has_table(table):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


async def _migrate(conn) -> None:
    for table, new_cols in _NEW_COLUMNS_BY_TABLE.items():
        existing = await conn.run_sync(_existing_columns, table)
        if not existing:
            # Table doesn't exist yet — create_all will handle it.
            continue
        for col, sql_type in new_cols.items():
            if col not in existing:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")
                )


async def init_db() -> None:
    from . import models  # noqa: F401  ensure models are registered

    async with engine.begin() as conn:
        # WAL mode lets multiple concurrent transactions read while one writes.
        # Critical for our background processor (multiple docs in flight at once).
        if settings.database_url.startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=10000"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)
