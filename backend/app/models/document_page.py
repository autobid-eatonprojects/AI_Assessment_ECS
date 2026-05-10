import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_doc_page"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    page_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-indexed
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)

    image_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    thumbnail_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Per-page text content. Sourced from PyMuPDF for digital PDFs OR from
    # OCR (Gemini vision) for scanned PDFs. The chunker reads from here, so
    # downstream Phase 3 indexing doesn't care which path produced it.
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # text_source: "pymupdf" | "ocr-gemini" | "ocr-anthropic" | None

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped["Document"] = relationship(back_populates="pages")  # noqa: F821
