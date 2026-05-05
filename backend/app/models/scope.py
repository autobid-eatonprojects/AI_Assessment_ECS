"""Models for Phase 4.3 + 4.4 — extracted Scope of Work.

Three tables, normalized:
    - ScopeExtractionRun: one per "Generate Scope" click. Audit + reproducibility.
    - ScopeItem: one per discrete biddable item. Belongs to a run.
    - ScopeCitation: one per (item, source chunk). Many-to-one to ScopeItem.

Citations carry the page + bbox + extraction-query metadata needed to
deep-link from the UI back to the source page.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScopeExtractionRun(Base):
    """One per click of 'Generate Scope of Work'."""

    __tablename__ = "scope_extraction_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    # running | complete | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-stage progress (driven by orchestrator, surfaced in UI)
    sections_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sections_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sections_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Stats (final)
    candidates_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_validated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_after_dedupe: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Reproducibility — snapshot of config at run start
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ScopeItem(Base):
    """A single biddable scope-of-work line item."""

    __tablename__ = "scope_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("scope_extraction_runs.id", ondelete="CASCADE"), index=True
    )

    # CSI grounding (always present after Phase 4.3 strict-grounding step)
    csi_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # "03 30 00"
    csi_division: Mapped[str] = mapped_column(String(8), nullable=False, index=True)  # "03"
    division_label: Mapped[str] = mapped_column(String(255), nullable=False)
    section_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The deliverable
    description: Mapped[str] = mapped_column(Text, nullable=False)
    specification: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Quality signals
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)  # 0..1
    # schedule | note | spec_section | plan_callout | inferred
    extraction_method: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Audit
    raw_extractions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    validation_votes: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    citations: Mapped[list["ScopeCitation"]] = relationship(
        back_populates="scope_item",
        cascade="all, delete-orphan",
    )


class ScopeCitation(Base):
    """A pointer from a scope item to a source chunk in the index."""

    __tablename__ = "scope_citations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope_item_id: Mapped[str] = mapped_column(
        ForeignKey("scope_items.id", ondelete="CASCADE"), index=True
    )

    # Loose reference to chunks.id (Phase 3 search index). Not a hard FK
    # because chunks may be re-indexed and we want the citation to remain
    # readable even if the chunk row is rewritten.
    chunk_id: Mapped[str] = mapped_column(String(36), nullable=False)

    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    rerank_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    extraction_query: Mapped[str | None] = mapped_column(String(64), nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    scope_item: Mapped["ScopeItem"] = relationship(back_populates="citations")
