"""Models that hold the structured output of the per-page vision pre-pass.

One PageExtraction row per (document, page). The actual extracted content
lives in normalised child tables so we can query them later (e.g. "give me
all FootingSchedule rows in the project"). The full raw LLM response is also
persisted on the parent row so we can re-derive child rows if the schema ever
changes — important for audit trails on a $20M project.
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


class PageExtraction(Base):
    """One per (document, page). Top-level metadata + raw LLM response."""

    __tablename__ = "page_extractions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[str] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"), index=True, unique=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Sheet metadata extracted from the page itself
    sheet_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sheet_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discipline: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    drawing_scale: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Status: pending | extracting | ready | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audit / debugging
    raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    schedules: Mapped[list["ExtractedSchedule"]] = relationship(
        back_populates="page_extraction",
        cascade="all, delete-orphan",
    )
    notes: Mapped[list["ExtractedNote"]] = relationship(
        back_populates="page_extraction",
        cascade="all, delete-orphan",
    )
    cross_references: Mapped[list["ExtractedCrossReference"]] = relationship(
        back_populates="page_extraction",
        cascade="all, delete-orphan",
    )
    entities: Mapped[list["ExtractedEntity"]] = relationship(
        back_populates="page_extraction",
        cascade="all, delete-orphan",
    )


class ExtractedSchedule(Base):
    """A structured table extracted from a drawing (footing, door, finish, etc.)."""

    __tablename__ = "extracted_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    page_extraction_id: Mapped[str] = mapped_column(
        ForeignKey("page_extractions.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    columns: Mapped[list] = mapped_column(JSON, nullable=False)  # list[str]
    rows: Mapped[list] = mapped_column(JSON, nullable=False)  # list[dict]
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    page_extraction: Mapped[PageExtraction] = relationship(back_populates="schedules")


class ExtractedNote(Base):
    """A general-notes paragraph extracted from a drawing."""

    __tablename__ = "extracted_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    page_extraction_id: Mapped[str] = mapped_column(
        ForeignKey("page_extractions.id", ondelete="CASCADE"), index=True
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    page_extraction: Mapped[PageExtraction] = relationship(back_populates="notes")


class ExtractedCrossReference(Base):
    """A reference to another sheet or detail (e.g., 'see S2.1', 'detail 5/A5.2')."""

    __tablename__ = "extracted_cross_references"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    page_extraction_id: Mapped[str] = mapped_column(
        ForeignKey("page_extractions.id", ondelete="CASCADE"), index=True
    )

    target_sheet: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detail_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    page_extraction: Mapped[PageExtraction] = relationship(back_populates="cross_references")


class ExtractedEntity(Base):
    """Fine-grained tagged identifier on a drawing (material, code, dimension, etc.)."""

    __tablename__ = "extracted_entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    page_extraction_id: Mapped[str] = mapped_column(
        ForeignKey("page_extractions.id", ondelete="CASCADE"), index=True
    )

    # material | manufacturer | code | dimension | room | equipment | symbol | other
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    page_extraction: Mapped[PageExtraction] = relationship(back_populates="entities")


class LLMCall(Base):
    """Audit log of every LLM call. Drives cost tracking + post-hoc debugging."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    project_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    page_extraction_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)

    purpose: Mapped[str] = mapped_column(String(64), nullable=False)  # classify | extract | ...
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="anthropic")

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
