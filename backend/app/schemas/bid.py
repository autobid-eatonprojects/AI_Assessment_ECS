"""API schemas for Phase 8 — bid analysis."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BidLineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    bid_document_id: str
    description: str
    quantity: str | None
    unit: str | None
    unit_price_usd: float | None
    total_price_usd: float | None
    csi_section_guess: str | None
    page_number: int | None


class BidExclusionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    bid_document_id: str
    text: str
    page_number: int | None


class BidInclusionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    bid_document_id: str
    text: str
    page_number: int | None


class BidSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    run_id: str
    bid_document_id: str
    vendor_name: str | None
    bid_total_usd: float | None
    primary_csi_divisions: list[str] | None
    line_item_count: int
    inclusion_count: int
    exclusion_count: int
    extraction_cost_usd: float | None
    extraction_latency_ms: int | None


class BidCoverageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scope_item_id: str
    bid_document_id: str
    status: str  # covered | partial | excluded | not_covered | not_applicable
    confidence: float
    reasoning: str | None
    matched_line_item_id: str | None
    judge_model: str | None


class BidRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    scope_run_id: str | None
    status: str  # running | complete | failed
    started_at: datetime
    completed_at: datetime | None
    error: str | None

    bids_total: int
    bids_extracted: int
    coverage_pairs_total: int
    coverage_pairs_completed: int

    total_cost_usd: float
    total_latency_ms: int

    config: dict | None


class BidAnalysisOverview(BaseModel):
    """Project-level bid-analysis summary."""

    project_id: str
    latest_run: BidRunOut | None
    bid_summaries: list[BidSummaryOut]
    coverage_counts: dict  # {covered: N, partial: N, excluded: N, not_covered: N}


class BidDetailOut(BaseModel):
    """Per-bid drill-down: line items + inclusions + exclusions + summary."""

    summary: BidSummaryOut
    line_items: list[BidLineItemOut]
    inclusions: list[BidInclusionOut]
    exclusions: list[BidExclusionOut]
