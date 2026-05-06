"""API schemas for Stage 7 — HITL review queues + audit + packages."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


# -----------------------------------------------------------------------------
# Conflicts
# -----------------------------------------------------------------------------


class ConflictMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scope_item_id: str
    role: str  # primary | contradictor
    citation_id: str | None
    is_winner: bool | None


class ConflictItemSummary(BaseModel):
    """Embedded scope item snapshot for the review drawer side-by-side."""

    id: str
    csi_code: str
    csi_division: str
    description: str
    quantity: str | None
    unit: str | None
    confidence: float
    evidence_tier: str | None


class ConflictOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    run_id: str
    conflict_type: str
    csi_division: str | None
    status: str
    arbitrated_value: dict | None
    arbitration_reasoning: str | None
    arbitrator: str | None
    created_at: datetime
    resolved_at: datetime | None
    members: list[ConflictMemberOut] = []
    # Hydrated separately (not on the SQLAlchemy model) so the UI can render
    # the side-by-side without a second round-trip
    item_snapshots: list[ConflictItemSummary] = []


class ConflictResolveIn(BaseModel):
    winner_member_id: str
    note: str | None = None


# -----------------------------------------------------------------------------
# Gaps
# -----------------------------------------------------------------------------


class GapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    run_id: str
    gap_type: str
    csi_division: str | None
    csi_section: str | None
    description: str
    severity: str
    suggested_remediation: str | None
    related_item_id: str | None
    status: str
    created_at: datetime
    acknowledged_at: datetime | None


class GapAcknowledgeIn(BaseModel):
    note: str | None = None


# -----------------------------------------------------------------------------
# Trade packages
# -----------------------------------------------------------------------------


class TradePackageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    run_id: str
    package_key: str
    package_label: str
    csi_divisions: list | None
    bundling_rule_source: str
    item_count: int
    bilateral_count: int
    avg_confidence: float | None
    narrative_md: str | None
    created_at: datetime


class TradePackageDetailOut(TradePackageOut):
    """Same as TradePackageOut plus the items grouped by csi_section."""

    items_by_section: list[dict] = []  # [{csi_section, section_title, items: [...]}]


class PackageItemMoveIn(BaseModel):
    target_package_id: str
    note: str | None = None


# -----------------------------------------------------------------------------
# Audit log
# -----------------------------------------------------------------------------


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    run_id: str | None
    entity_type: str
    entity_id: str | None
    action: str
    actor: str
    payload: dict | None
    note: str | None
    created_at: datetime


# -----------------------------------------------------------------------------
# Item reclassification
# -----------------------------------------------------------------------------


class ItemReclassifyIn(BaseModel):
    new_csi_code: str
    note: str | None = None
