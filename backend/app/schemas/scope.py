"""API schemas for Phase 4.3/4.4 — scope extraction."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ScopeCitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chunk_id: str
    document_id: str | None
    page_number: int | None
    sheet_number: str | None
    bbox: dict | None
    rerank_score: float | None
    extraction_query: str | None
    excerpt: str | None
    # Stage 1/3 — denormalized source-type + link-judge entailment
    evidence_type: str | None = None
    is_link_judge_pass: bool | None = None
    link_judge_score: float | None = None


class ScopeItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    run_id: str

    csi_code: str
    csi_division: str
    division_label: str
    section_title: str | None

    description: str
    specification: str | None
    quantity: str | None
    unit: str | None
    location: str | None

    confidence: float
    extraction_method: str | None

    qty_confidence: str | None = None
    qty_provenance: dict | None = None

    verifier_status: str | None = None
    verifier_review: dict | None = None

    # Stage 2 — bilateral evidence + tier
    evidence_tier: str | None = None
    bilateral_evidence: bool | None = None
    trust_components: dict | None = None

    # Stage 4 — trade bundling
    package_id: str | None = None

    citations: list[ScopeCitationOut]

    created_at: datetime
    updated_at: datetime


class ScopeRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    error: str | None

    sections_total: int
    sections_completed: int
    sections_failed: int

    candidates_generated: int
    items_validated: int
    items_after_dedupe: int
    total_cost_usd: float
    total_latency_ms: int

    config: dict | None

    # Stage 6 — trust score + run-level rollups
    trust_score: float | None = None
    trust_score_components: dict | None = None
    bilateral_coverage_rate: float | None = None
    link_judge_pass_rate: float | None = None
    spec_section_coverage_rate: float | None = None
    conflict_count: int | None = None
    gap_count: int | None = None
    package_count: int | None = None


class TrustScoreOut(BaseModel):
    """Trust score view for the dashboard / scope page header."""

    score: float
    tier: str
    components: dict
    weights: dict
    tier_thresholds: dict
    dropped_components: list[str] = []
    rationale: str = ""


class ScopeOverview(BaseModel):
    """Project-level scope summary — used for the tree + counts in the UI."""

    project_id: str
    latest_run: ScopeRunOut | None
    total_items: int
    by_division: list[dict]  # [{csi_division, division_label, count}]
