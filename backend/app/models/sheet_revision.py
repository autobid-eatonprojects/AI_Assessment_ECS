"""Revision-block entries from each sheet's title block (W18 mitigation).

Every architectural / engineering sheet has a revision block (usually
right side of the title block) listing every revision: revision number,
issue date, brief description, and reviewer initials. We extract these
into SheetRevision rows so:

  - The sheet manifest can show "Sheet A1.1 Rev 0 vs Rev 2" when the
    same drawing set has stale + current copies (W12 / W18).
  - The HITL UI can highlight revision clouds for the operator's
    attention.
  - The trust score's document_version_consistency component (5%) gets
    real signal: if revisions across sheets are inconsistent (some Rev 0,
    some Rev 2 with no addendum noted), drop the score.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SheetRevision(Base):
    """One row per (sheet, revision-entry) — many revisions per sheet."""

    __tablename__ = "sheet_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_extraction_id: Mapped[str | None] = mapped_column(
        ForeignKey("page_extractions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # The sheet this revision is for (e.g. "S1.1"). Cross-references to
    # SheetIndex.sheet_id but we store flat (no FK) since SheetIndex is
    # per-document and may not have caught this sheet.
    sheet_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Revision number AS PRINTED — usually integer ("0", "1", "2") but
    # sometimes alpha ("A", "B", "C") on the very first issues. Keep raw.
    rev_number: Mapped[str] = mapped_column(String(16), nullable=False)
    rev_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Position in the title block's revision list, 1-indexed. Useful for
    # ordering when the rev_number is alpha or non-numeric.
    rev_order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
