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


class ScopeOverview(BaseModel):
    """Project-level scope summary — used for the tree + counts in the UI."""

    project_id: str
    latest_run: ScopeRunOut | None
    total_items: int
    by_division: list[dict]  # [{csi_division, division_label, count}]
