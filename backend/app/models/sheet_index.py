"""Authoritative sheet manifest extracted from a drawing-set cover sheet.

Different from `PageExtraction.sheet_number` (which is per-page best-effort
detection): the SheetIndex is the *master list* from the cover sheet (CVR /
G-001). It tells you every sheet that exists in the drawing set + its
discipline + its position in the set order, regardless of whether the
per-page vision extractor managed to catch every sheet number.

Used by:
  - P3 discipline agents (filter chunks to a discipline's sheets)
  - Cross-reference validator (a `see S2.1` in a drawing is a real link
    iff S2.1 is in the index — otherwise it's a W8 cross-ref failure)
  - Sheet manifest in HITL UI (operator sees expected vs received sheets)
  - W12 mitigation (revision-date inconsistency surfaces here)
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SheetIndex(Base):
    """One row per sheet listed on the project's drawing-set cover sheet."""

    __tablename__ = "sheet_index"
    __table_args__ = (
        # A project may legitimately have the same sheet_id appear in
        # different document_ids (revision sets) — uniqueness is per
        # document, not per project.
        UniqueConstraint(
            "document_id", "sheet_id", name="uq_sheet_index_doc_sheet"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    # The sheet number as printed on the cover sheet (e.g. "A1.1", "S2.3",
    # "M0.1", "FP-101"). Discipline is derived from the prefix character
    # but recorded explicitly for cases where the prefix doesn't match the
    # standard convention (rare but happens on retrofit projects).
    sheet_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # architectural | structural | mechanical | electrical | plumbing |
    # fire-protection | civil | site | interior | other
    discipline: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Position in the set per the cover sheet's listed order. Lets us
    # reconstruct the set order without referring back to PDF page numbers.
    set_order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Optional revision marker as printed in the index (Rev #, date)
    revision: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
