"""Phase 8 — Bid Analysis models.

Three normalized tables:

- BidExtractionRun: one per "Run bid analysis" click. Tracks status, totals,
  cost, and error per run for audit + reproducibility.
- BidLineItem: one per (bid_document, line item). Carries description,
  quantity, unit, unit_price, total_price, and a CSI-section guess.
- BidExclusion / BidInclusion: explicit "Excludes:" / "Includes:" entries
  parsed from the bid text. Critical for the coverage matrix — a bid that
  *excludes* an item should be flagged red, not just absent.
- BidCoverage: one per (scope_item, bid_document). Says whether this bid
  covers that scope item, with evidence + reasoning for audit.

We persist everything ScopeItem-style with citations back to the source
chunk so the UI can deep-link from a coverage cell to the originating
PDF page.
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


class BidExtractionRun(Base):
    """One per 'Run bid analysis' click — covers extraction + coverage matrix."""

    __tablename__ = "bid_extraction_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # Optional FK to a scope run — analysis is meaningful only against a
    # specific scope. Null only on legacy data.
    scope_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("scope_extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # running | complete | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Stage progress (driven by orchestrator, surfaced in UI)
    bids_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bids_extracted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coverage_pairs_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coverage_pairs_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Final cost / latency
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Reproducibility
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BidLineItem(Base):
    """One priced line item parsed from a vendor bid."""

    __tablename__ = "bid_line_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("bid_extraction_runs.id", ondelete="CASCADE"), index=True
    )
    bid_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    unit_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # CSI section the line probably belongs to (best-effort; null if unknown)
    csi_section_guess: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    # Source attribution
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chunk_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BidExclusion(Base):
    """An explicit 'Excludes:' entry parsed from a bid."""

    __tablename__ = "bid_exclusions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("bid_extraction_runs.id", ondelete="CASCADE"), index=True
    )
    bid_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chunk_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BidInclusion(Base):
    """An explicit 'Includes:' entry parsed from a bid (often clarifies scope)."""

    __tablename__ = "bid_inclusions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("bid_extraction_runs.id", ondelete="CASCADE"), index=True
    )
    bid_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chunk_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BidSummary(Base):
    """Per-bid roll-up: total price, vendor, division coverage hint."""

    __tablename__ = "bid_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("bid_extraction_runs.id", ondelete="CASCADE"), index=True
    )
    bid_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True, unique=False
    )

    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bid_total_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    primary_csi_divisions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    line_item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inclusion_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exclusion_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extraction_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    extraction_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BidCoverage(Base):
    """Coverage decision for one (scope_item, bid_document) pair."""

    __tablename__ = "bid_coverage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("bid_extraction_runs.id", ondelete="CASCADE"), index=True
    )
    scope_item_id: Mapped[str] = mapped_column(
        ForeignKey("scope_items.id", ondelete="CASCADE"), index=True
    )
    bid_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    # covered | partial | excluded | not_covered | not_applicable
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_covered")
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional FK to the bid line that covers this scope item (when status=covered)
    matched_line_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("bid_line_items.id", ondelete="SET NULL"), nullable=True
    )

    # Audit: which Haiku/Sonnet decided + which chunk_ids were inspected
    judge_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_chunk_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
