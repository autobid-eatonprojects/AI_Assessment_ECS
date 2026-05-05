"""Search index chunks.

A chunk is the atomic unit of retrieval. Every schedule, note, cross-reference,
entity, and page-text block becomes one chunk. The canonical text + metadata
lives in this table; vector embeddings live in Chroma keyed by `chunk.id`. We
keep them split so the DB stays the source of truth for audit / re-indexing
and Chroma is just a fast nearest-neighbour cache.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"), index=True, nullable=True
    )
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # schedule | note | cross_reference | entity | page_summary | page_text
    chunk_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # FK to the source row (loose — we don't enforce as FK to keep it polymorphic)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    contextualized_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    embedded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
