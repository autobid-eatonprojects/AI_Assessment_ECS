"""API schemas for Phase 11 — vendor profile + bid leveling."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class VendorDocSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    doc_type: str | None
    classification_confidence: float | None
    processing_status: str
    page_count: int | None


class VendorQualifications(BaseModel):
    """Bool checklist surfacing the canonical qualification document types."""

    has_license_or_insurance: bool
    has_safety_manual: bool
    has_contractor_info: bool
    is_complete: bool  # license + insurance + safety all present
    license_or_insurance_count: int
    safety_manual_count: int
    contractor_info_count: int


class VendorCoverageStats(BaseModel):
    """Per-vendor breakdown of how their bid maps onto the GC's scope."""

    covered: int
    partial: int
    excluded: int
    not_covered: int


class VendorSummary(BaseModel):
    """Card-level summary, one per canonical vendor."""

    canonical_vendor: str
    primary_csi_divisions: list[str]
    bid_total_usd: float | None
    line_item_count: int
    inclusion_count: int
    exclusion_count: int
    document_count: int
    has_priced_bid: bool  # at least one doc_type=bid-quote/scope-letter
    qualifications: VendorQualifications
    coverage: VendorCoverageStats | None  # null if no scope or no bid

    # Aliases — every variant of the vendor name we saw across docs
    aliases: list[str]


class VendorProfileResponse(BaseModel):
    """Full vendor profile: summary + line items + exclusions + inclusions + docs."""

    canonical_vendor: str
    primary_csi_divisions: list[str]
    bid_total_usd: float | None
    line_item_count: int
    inclusion_count: int
    exclusion_count: int
    qualifications: VendorQualifications
    coverage: VendorCoverageStats | None
    aliases: list[str]

    documents: list[VendorDocSummary]
    line_items: list[dict]  # serialized BidLineItem
    inclusions: list[dict]  # serialized BidInclusion
    exclusions: list[dict]  # serialized BidExclusion

    # For drill-down: the scope items this vendor covers / excludes / leaves uncovered
    covered_scope_item_ids: list[str]
    excluded_scope_item_ids: list[str]
    not_covered_scope_item_ids: list[str]
    partial_scope_item_ids: list[str]


class BidLevelingCell(BaseModel):
    """One cell in the bid leveling table."""

    vendor: str  # canonical_vendor
    status: str  # covered | partial | excluded | not_covered | not_applicable
    matched_line_description: str | None
    matched_line_total_usd: float | None
    confidence: float
    reasoning: str | None


class BidLevelingRow(BaseModel):
    """One scope item row across all vendor columns."""

    scope_item_id: str
    csi_code: str
    description: str
    quantity: str | None
    unit: str | None
    cells: list[BidLevelingCell]  # one per vendor in `vendors`


class BidLevelingResponse(BaseModel):
    """Side-by-side comparison of all bidders for a CSI division."""

    csi_division: str | None
    vendors: list[str]  # canonical_vendor names, column order
    rows: list[BidLevelingRow]
