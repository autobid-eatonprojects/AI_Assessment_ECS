"""Search index chunks.

A chunk is the atomic unit of retrieval. Every schedule, note, cross-reference,
entity, and page-text block becomes one chunk. The canonical text + metadata
AND the dense embedding both live in Postgres — pgvector keeps the embedding
in this same table so retrieval is a single SQL query (ORDER BY <-> LIMIT)
with ACID joins to the rest of the schema. No separate Chroma store.
"""

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Embedding dimension — voyage-3-large per the design doc.
EMBEDDING_DIM = 1024


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        # HNSW index for fast pgvector ANN. Declared here so Alembic
        # autogenerate doesn't repeatedly try to drop it (since it lives
        # in the live DB but couldn't be reflected from the model
        # otherwise). The actual CREATE statement lives in the
        # `5cd4987189b2_add_hnsw_index_on_chunks_embedding` migration.
        Index(
            "chunks_embedding_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

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

    # Dense embedding lives here in pgvector. Nullable so chunk rows can be
    # written before embedding completes (the embedder backfills). The HNSW
    # index defined in the Alembic migration accelerates ANN over this column.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )
    embedded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
