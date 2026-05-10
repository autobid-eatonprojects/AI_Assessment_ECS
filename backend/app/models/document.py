import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Two-stage workflow:
    #   project_document — uploaded by the GC during project setup (drawings,
    #                       specs, trade list). Feeds Phase 4 scope extraction.
    #   bid_submission   — uploaded after scope is locked, comes from a vendor.
    #                       Feeds Phase 8 bid analysis.
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="project_document", index=True
    )
    # Free-text vendor identifier on bid submissions only. Initially typed
    # by the operator at upload; overwritten by the classifier when it can
    # read a clearer name from the document letterhead. Null on project docs.
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Phase 11: post-canonicalization vendor key — variants like 'TLC',
    # 'Tennessee Lawn Care', and 'The Cleaning Leaders LLC' all collapse to
    # the same canonical_vendor so the vendor profile page groups cleanly.
    canonical_vendor: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Phase 11: audit trail for the vendor name — what the operator typed,
    # what the classifier detected, and the canonical form chosen.
    vendor_provenance: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Classification (Phase 1)
    doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    classification_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Page rendering (Phase 1)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Background processing status:
    # pending | classifying | rendering | ready | failed | needs-api-key
    processing_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    project: Mapped["Project"] = relationship(back_populates="documents")  # noqa: F821
    pages: Mapped[list["DocumentPage"]] = relationship(  # noqa: F821
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentPage.page_number",
    )
