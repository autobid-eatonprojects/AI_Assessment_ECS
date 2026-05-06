"""Project-specific symbol legend (W3 mitigation, P3 prep).

Every drawing set has a Symbols & Abbreviations sheet — usually in the
G-series, sometimes embedded on individual discipline cover sheets
(M-001, E-001, etc.). The legend pairs symbols with their meanings
specific to THIS project's CAD library.

Why we ingest it:
  - Vision LLMs (and YOLO classifiers) can't reliably read project-
    specific symbol variants out of the box; ingesting the legend
    upfront gives us ground-truth class labels for that project's
    vision pass (W3 mitigation, "highest leverage" per design doc)
  - Discipline agents reference the legend when reading drawings,
    so a "PG" symbol in the FP discipline gets recognized as
    "Pre-Action Sprinkler Riser" not as ambiguous text

Schema:
  - symbol_glyph: short identifier as printed (e.g. "PG", "▽", "A1")
  - meaning: full text expansion (e.g. "Pre-Action Sprinkler Riser")
  - discipline: which discipline the symbol belongs to (architectural,
    structural, mechanical, electrical, plumbing, fire-protection)
  - source_sheet_id: which sheet defined this symbol
  - csi_section_hint: best-guess CSI section the symbol implies (e.g.
    a toilet symbol → "22 40 00")
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SymbolLegend(Base):
    """One entry per (project, symbol_glyph, discipline)."""

    __tablename__ = "symbol_legend"

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

    # The printed symbol. Could be a letter ('A1'), a glyph ('▽'),
    # a short code ('PG'), or a 3-5 word verbal description of a
    # graphical glyph ("rectangle with diagonal lines and opposing
    # arrows inside"). Stored verbatim from the legend or vision pass.
    symbol_glyph: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    # Full text expansion (e.g. "Pre-Action Sprinkler Riser",
    # "Floor Drain", "Detail Reference")
    meaning: Mapped[str] = mapped_column(Text, nullable=False)

    # arch / struct / mech / elec / plumb / fire-prot / civil / general
    discipline: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_sheet_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Best-guess CSI section the symbol implies — used by the link
    # judge / scope extractor to suggest a CSI bucket when a drawing
    # element only carries the symbol with no other context.
    csi_section_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
