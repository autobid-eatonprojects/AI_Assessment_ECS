"""Phase 4/5/6 — Conflict, Gap, and TradePackage tables.

These three are the surfaces the HITL queue and trade bundling stages
populate. Each row is run-scoped (FK to ScopeExtractionRun); deleting a
run cascades to its review state.

    Conflict + ConflictMember — clusters of scope items in disagreement.
        Surfaced in the Review queue (Conflicts tab). Resolved manually
        or by the auto-arbitrator.

    Gap — first-class "what's missing" record. Types: missing-division,
        missing-section, unilateral-evidence, unresolved-cross-reference.

    TradePackage + TradePackageItem — AGC-style rollup of scope items
        into bid packages, driven by `bundling_rules.yaml` plus
        per-project overrides.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------------------
# Conflict
# -----------------------------------------------------------------------------


class Conflict(Base):
    """A cluster of scope items detected as disagreeing on the same claim."""

    __tablename__ = "scope_conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("scope_extraction_runs.id", ondelete="CASCADE"), index=True
    )

    # qty_mismatch | unit_mismatch | spec_contradiction | cross_division_overlap
    conflict_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Optional CSI division pin (set for within-division conflicts; null for
    # cross-division overlap clusters that span multiple divisions).
    csi_division: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)

    # open | resolved | deferred | ignored
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", index=True
    )

    # Once resolved: the chosen winner's value (e.g., the kept qty/unit/spec)
    arbitrated_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    arbitration_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # haiku-judge | opus-verifier | operator
    arbitrator: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    members: Mapped[list["ConflictMember"]] = relationship(
        back_populates="conflict",
        cascade="all, delete-orphan",
    )


class ConflictMember(Base):
    """One scope item participating in a Conflict (M:N)."""

    __tablename__ = "scope_conflict_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conflict_id: Mapped[str] = mapped_column(
        ForeignKey("scope_conflicts.id", ondelete="CASCADE"), index=True
    )
    scope_item_id: Mapped[str] = mapped_column(
        ForeignKey("scope_items.id", ondelete="CASCADE"), index=True
    )
    # primary | contradictor (informational; arbitrator picks the actual winner)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="primary")

    # Optional pointer to the citation that grounded the disagreeing claim
    citation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Set when the arbitrator picks this member as the winner
    is_winner: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    conflict: Mapped["Conflict"] = relationship(back_populates="members")


# -----------------------------------------------------------------------------
# Gap
# -----------------------------------------------------------------------------


class Gap(Base):
    """A 'what's missing' record surfaced for HITL review."""

    __tablename__ = "scope_gaps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("scope_extraction_runs.id", ondelete="CASCADE"), index=True
    )

    # missing_division | missing_section | unilateral_evidence | unresolved_cross_reference
    gap_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)

    # Optional CSI scope pins (null when gap is project-wide)
    csi_division: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    csi_section: Mapped[str | None] = mapped_column(String(16), nullable=True)

    description: Mapped[str] = mapped_column(Text, nullable=False)

    # blocker | warn | info
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="warn", index=True
    )
    suggested_remediation: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional pointer to the scope item or citation that triggered the gap
    related_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # open | acknowledged | resolved
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# -----------------------------------------------------------------------------
# TradePackage
# -----------------------------------------------------------------------------


class TradePackage(Base):
    """An AGC-style rollup of scope items into a single bid package."""

    __tablename__ = "trade_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("scope_extraction_runs.id", ondelete="CASCADE"), index=True
    )

    # Stable key for the package within a run, e.g. "concrete-masonry"
    package_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    package_label: Mapped[str] = mapped_column(String(255), nullable=False)

    # Which CSI divisions feed this package, e.g. ["03", "04"]
    csi_divisions: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # yaml | override (project-scoped customer override took precedence)
    bundling_rule_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="yaml"
    )

    # Denormalized for cheap UI rendering — refreshed by trade_bundler
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bilateral_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Optional Haiku-drafted narrative for the bid invitation cover letter.
    # Populated lazily by Stage 10 (output generation), not Stage 4 (bundling).
    narrative_md: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TradePackageItem(Base):
    """M:N — scope item ↔ trade package, with assignment provenance."""

    __tablename__ = "trade_package_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("trade_packages.id", ondelete="CASCADE"), index=True
    )
    scope_item_id: Mapped[str] = mapped_column(
        ForeignKey("scope_items.id", ondelete="CASCADE"), index=True
    )
    # primary_division | manual_override
    assignment_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="primary_division"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
