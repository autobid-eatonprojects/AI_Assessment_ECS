"""Phase 11 endpoints — Vendor Profile + Bid Leveling.

The data model already carries everything we need:
  - Document.canonical_vendor groups bid_submission docs by vendor
  - BidSummary / BidLineItem / BidExclusion / BidInclusion → priced-bid data
  - BidCoverage → per-(scope_item, vendor) coverage decisions

These endpoints aggregate that data per vendor for the UI. No new LLM
calls; pure DB queries.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..models import (
    BidCoverage,
    BidExclusion,
    BidExtractionRun,
    BidInclusion,
    BidLineItem,
    BidSummary,
    Document,
    Project,
    ScopeExtractionRun,
    ScopeItem,
)
from ..schemas import (
    BidLevelingCell,
    BidLevelingResponse,
    BidLevelingRow,
    VendorCoverageStats,
    VendorDocSummary,
    VendorProfileResponse,
    VendorQualifications,
    VendorSummary,
)
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/vendors", tags=["vendors"])


# Categorize doc_type → qualification kind so the checklist groups them
# without a separate doc-type-to-category lookup table.
_LICENSE_INSURANCE_TYPES = {"license-insurance"}
_SAFETY_TYPES = {"safety-manual"}
_CONTRACTOR_INFO_TYPES = {"contractor-info"}
_PRICED_BID_TYPES = {"bid-quote", "scope-letter"}


async def _ensure_project(db, project_id: str) -> Project:
    p = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return p


def _qualifications_from_docs(docs: list[Document]) -> VendorQualifications:
    counts: Counter[str] = Counter()
    for d in docs:
        if d.doc_type in _LICENSE_INSURANCE_TYPES:
            counts["license"] += 1
        elif d.doc_type in _SAFETY_TYPES:
            counts["safety"] += 1
        elif d.doc_type in _CONTRACTOR_INFO_TYPES:
            counts["contractor"] += 1
    has_license = counts.get("license", 0) > 0
    has_safety = counts.get("safety", 0) > 0
    has_contractor = counts.get("contractor", 0) > 0
    return VendorQualifications(
        has_license_or_insurance=has_license,
        has_safety_manual=has_safety,
        has_contractor_info=has_contractor,
        is_complete=has_license and has_safety,
        license_or_insurance_count=counts.get("license", 0),
        safety_manual_count=counts.get("safety", 0),
        contractor_info_count=counts.get("contractor", 0),
    )


@router.get("", response_model=list[VendorSummary])
async def list_vendors(
    project_id: str, db: DB, _: CurrentUser
) -> list[VendorSummary]:
    """All canonical vendors on the project, with summary stats per card."""
    await _ensure_project(db, project_id)

    docs = (
        await db.execute(
            select(Document)
            .where(Document.project_id == project_id)
            .where(Document.source == "bid_submission")
            .where(Document.canonical_vendor.is_not(None))
        )
    ).scalars().all()

    if not docs:
        return []

    # Group docs by canonical vendor
    by_vendor: dict[str, list[Document]] = defaultdict(list)
    aliases_by_vendor: dict[str, set[str]] = defaultdict(set)
    for d in docs:
        by_vendor[d.canonical_vendor].append(d)
        if d.vendor_name:
            aliases_by_vendor[d.canonical_vendor].add(d.vendor_name)

    # Pull latest bid run + coverage so we can attach stats
    latest_bid_run = (
        await db.execute(
            select(BidExtractionRun)
            .where(BidExtractionRun.project_id == project_id)
            .order_by(BidExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    bid_summaries_by_doc: dict[str, BidSummary] = {}
    coverage_counts_by_doc: dict[str, Counter[str]] = defaultdict(Counter)
    if latest_bid_run is not None:
        for s in (
            await db.execute(
                select(BidSummary).where(BidSummary.run_id == latest_bid_run.id)
            )
        ).scalars().all():
            bid_summaries_by_doc[s.bid_document_id] = s
        for cov in (
            await db.execute(
                select(BidCoverage).where(BidCoverage.run_id == latest_bid_run.id)
            )
        ).scalars().all():
            coverage_counts_by_doc[cov.bid_document_id][cov.status] += 1

    summaries: list[VendorSummary] = []
    for canon, vendor_docs in sorted(by_vendor.items()):
        # Aggregate bid summary across all priced docs for this vendor
        priced = [d for d in vendor_docs if d.doc_type in _PRICED_BID_TYPES]
        priced_summaries = [
            bid_summaries_by_doc[d.id]
            for d in priced
            if d.id in bid_summaries_by_doc
        ]
        primary_divs: set[str] = set()
        bid_total: float | None = None
        line_count = inc_count = exc_count = 0
        cov_counter: Counter[str] = Counter()
        for s in priced_summaries:
            primary_divs.update(s.primary_csi_divisions or [])
            if s.bid_total_usd is not None:
                bid_total = (bid_total or 0.0) + s.bid_total_usd
            line_count += s.line_item_count
            inc_count += s.inclusion_count
            exc_count += s.exclusion_count
            cov_counter += coverage_counts_by_doc.get(s.bid_document_id, Counter())

        summaries.append(
            VendorSummary(
                canonical_vendor=canon,
                primary_csi_divisions=sorted(primary_divs),
                bid_total_usd=bid_total,
                line_item_count=line_count,
                inclusion_count=inc_count,
                exclusion_count=exc_count,
                document_count=len(vendor_docs),
                has_priced_bid=len(priced) > 0,
                qualifications=_qualifications_from_docs(vendor_docs),
                coverage=(
                    VendorCoverageStats(
                        covered=cov_counter.get("covered", 0),
                        partial=cov_counter.get("partial", 0),
                        excluded=cov_counter.get("excluded", 0),
                        not_covered=cov_counter.get("not_covered", 0),
                    )
                    if cov_counter
                    else None
                ),
                aliases=sorted(aliases_by_vendor[canon]),
            )
        )
    return summaries


@router.get("/{vendor_name:path}", response_model=VendorProfileResponse)
async def get_vendor_profile(
    project_id: str, vendor_name: str, db: DB, _: CurrentUser
) -> VendorProfileResponse:
    """Full profile: summary stats + line items + exclusions + docs +
    drill-down lists of which scope items this vendor covers / excludes /
    misses. The vendor_name path param is the canonical_vendor string."""
    await _ensure_project(db, project_id)

    vendor_docs = (
        await db.execute(
            select(Document)
            .where(Document.project_id == project_id)
            .where(Document.source == "bid_submission")
            .where(Document.canonical_vendor == vendor_name)
        )
    ).scalars().all()
    if not vendor_docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"vendor '{vendor_name}' not found in this project",
        )

    aliases = sorted({d.vendor_name for d in vendor_docs if d.vendor_name})

    # Latest bid run for this project
    latest_bid_run = (
        await db.execute(
            select(BidExtractionRun)
            .where(BidExtractionRun.project_id == project_id)
            .order_by(BidExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    priced_doc_ids = [
        d.id for d in vendor_docs if d.doc_type in _PRICED_BID_TYPES
    ]
    line_items: list[BidLineItem] = []
    inclusions: list[BidInclusion] = []
    exclusions: list[BidExclusion] = []
    summaries: list[BidSummary] = []
    cov_rows: list[BidCoverage] = []

    if latest_bid_run is not None and priced_doc_ids:
        line_items = list(
            (
                await db.execute(
                    select(BidLineItem)
                    .where(BidLineItem.run_id == latest_bid_run.id)
                    .where(BidLineItem.bid_document_id.in_(priced_doc_ids))
                )
            ).scalars().all()
        )
        inclusions = list(
            (
                await db.execute(
                    select(BidInclusion)
                    .where(BidInclusion.run_id == latest_bid_run.id)
                    .where(BidInclusion.bid_document_id.in_(priced_doc_ids))
                )
            ).scalars().all()
        )
        exclusions = list(
            (
                await db.execute(
                    select(BidExclusion)
                    .where(BidExclusion.run_id == latest_bid_run.id)
                    .where(BidExclusion.bid_document_id.in_(priced_doc_ids))
                )
            ).scalars().all()
        )
        summaries = list(
            (
                await db.execute(
                    select(BidSummary)
                    .where(BidSummary.run_id == latest_bid_run.id)
                    .where(BidSummary.bid_document_id.in_(priced_doc_ids))
                )
            ).scalars().all()
        )
        cov_rows = list(
            (
                await db.execute(
                    select(BidCoverage)
                    .where(BidCoverage.run_id == latest_bid_run.id)
                    .where(BidCoverage.bid_document_id.in_(priced_doc_ids))
                )
            ).scalars().all()
        )

    # Roll up summary stats
    primary_divs: set[str] = set()
    bid_total: float | None = None
    for s in summaries:
        primary_divs.update(s.primary_csi_divisions or [])
        if s.bid_total_usd is not None:
            bid_total = (bid_total or 0.0) + s.bid_total_usd

    # Coverage breakdown
    cov_counter: Counter[str] = Counter()
    cov_by_status: dict[str, list[str]] = defaultdict(list)
    for c in cov_rows:
        cov_counter[c.status] += 1
        cov_by_status[c.status].append(c.scope_item_id)

    coverage_obj = (
        VendorCoverageStats(
            covered=cov_counter.get("covered", 0),
            partial=cov_counter.get("partial", 0),
            excluded=cov_counter.get("excluded", 0),
            not_covered=cov_counter.get("not_covered", 0),
        )
        if cov_counter
        else None
    )

    return VendorProfileResponse(
        canonical_vendor=vendor_name,
        primary_csi_divisions=sorted(primary_divs),
        bid_total_usd=bid_total,
        line_item_count=len(line_items),
        inclusion_count=len(inclusions),
        exclusion_count=len(exclusions),
        qualifications=_qualifications_from_docs(list(vendor_docs)),
        coverage=coverage_obj,
        aliases=aliases,
        documents=[
            VendorDocSummary.model_validate(d)
            for d in sorted(vendor_docs, key=lambda x: (x.doc_type or "", x.filename))
        ],
        line_items=[
            {
                "id": ln.id,
                "description": ln.description,
                "quantity": ln.quantity,
                "unit": ln.unit,
                "unit_price_usd": ln.unit_price_usd,
                "total_price_usd": ln.total_price_usd,
                "csi_section_guess": ln.csi_section_guess,
                "page_number": ln.page_number,
            }
            for ln in line_items
        ],
        inclusions=[{"id": i.id, "text": i.text, "page_number": i.page_number} for i in inclusions],
        exclusions=[{"id": e.id, "text": e.text, "page_number": e.page_number} for e in exclusions],
        covered_scope_item_ids=cov_by_status.get("covered", []),
        partial_scope_item_ids=cov_by_status.get("partial", []),
        excluded_scope_item_ids=cov_by_status.get("excluded", []),
        not_covered_scope_item_ids=cov_by_status.get("not_covered", []),
    )


# ─────── Bid leveling: side-by-side comparison ───────


bidleveling_router = APIRouter(
    prefix="/projects/{project_id}/bid-leveling", tags=["bid-leveling"]
)


@bidleveling_router.get("", response_model=BidLevelingResponse)
async def get_bid_leveling(
    project_id: str,
    db: DB,
    _: CurrentUser,
    csi_division: str | None = None,
) -> BidLevelingResponse:
    """Pivot the coverage matrix into a leveling table: scope items as
    rows, vendors as columns. Optionally filter to a specific division
    (e.g. compare only Div 01 cleaning bids).
    """
    await _ensure_project(db, project_id)

    latest_bid_run = (
        await db.execute(
            select(BidExtractionRun)
            .where(BidExtractionRun.project_id == project_id)
            .order_by(BidExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_bid_run is None or latest_bid_run.scope_run_id is None:
        return BidLevelingResponse(
            csi_division=csi_division, vendors=[], rows=[]
        )

    # Vendors active in this run (those with a BidSummary)
    summaries = list(
        (
            await db.execute(
                select(BidSummary).where(BidSummary.run_id == latest_bid_run.id)
            )
        ).scalars().all()
    )
    if not summaries:
        return BidLevelingResponse(
            csi_division=csi_division, vendors=[], rows=[]
        )

    # Resolve canonical_vendor for each summary's bid_document_id
    bid_doc_ids = [s.bid_document_id for s in summaries]
    docs = list(
        (
            await db.execute(
                select(Document).where(Document.id.in_(bid_doc_ids))
            )
        ).scalars().all()
    )
    canonical_by_doc = {d.id: (d.canonical_vendor or d.vendor_name or "(unknown)") for d in docs}

    # If a division filter is set, restrict to vendors that bid that division
    relevant_vendors: set[str] = set()
    for s in summaries:
        if csi_division is None or csi_division in (s.primary_csi_divisions or []):
            relevant_vendors.add(canonical_by_doc[s.bid_document_id])
    vendors = sorted(relevant_vendors)
    if not vendors:
        return BidLevelingResponse(
            csi_division=csi_division, vendors=[], rows=[]
        )

    # Pull all coverage rows for the run; index by (scope_item_id, vendor)
    cov_rows = list(
        (
            await db.execute(
                select(BidCoverage).where(BidCoverage.run_id == latest_bid_run.id)
            )
        ).scalars().all()
    )

    # Map matched_line_item_id → BidLineItem for cell values
    line_ids = {c.matched_line_item_id for c in cov_rows if c.matched_line_item_id}
    line_items_by_id: dict[str, BidLineItem] = {}
    if line_ids:
        line_items_by_id = {
            ln.id: ln
            for ln in (
                await db.execute(
                    select(BidLineItem).where(BidLineItem.id.in_(line_ids))
                )
            ).scalars().all()
        }

    cov_index: dict[tuple[str, str], BidCoverage] = {}
    for c in cov_rows:
        vendor = canonical_by_doc.get(c.bid_document_id) or "(unknown)"
        if vendor not in relevant_vendors:
            continue
        cov_index[(c.scope_item_id, vendor)] = c

    # Pull scope items for the anchored scope run; filter by division
    scope_q = (
        select(ScopeItem)
        .where(ScopeItem.run_id == latest_bid_run.scope_run_id)
        .order_by(ScopeItem.csi_code, ScopeItem.description)
    )
    if csi_division is not None:
        scope_q = scope_q.where(ScopeItem.csi_division == csi_division)
    scope_items = list((await db.execute(scope_q)).scalars().all())

    rows: list[BidLevelingRow] = []
    for si in scope_items:
        cells: list[BidLevelingCell] = []
        any_relevant = False
        for vendor in vendors:
            cov = cov_index.get((si.id, vendor))
            if cov is None:
                cells.append(
                    BidLevelingCell(
                        vendor=vendor,
                        status="not_applicable",
                        matched_line_description=None,
                        matched_line_total_usd=None,
                        confidence=0.0,
                        reasoning=None,
                    )
                )
                continue
            any_relevant = True
            line = (
                line_items_by_id.get(cov.matched_line_item_id)
                if cov.matched_line_item_id
                else None
            )
            cells.append(
                BidLevelingCell(
                    vendor=vendor,
                    status=cov.status,
                    matched_line_description=line.description if line else None,
                    matched_line_total_usd=line.total_price_usd if line else None,
                    confidence=cov.confidence,
                    reasoning=cov.reasoning,
                )
            )
        # Skip rows where no vendor in our set actually scored a coverage cell
        # (otherwise the division filter shows huge tables of all-not-applicable)
        if any_relevant:
            rows.append(
                BidLevelingRow(
                    scope_item_id=si.id,
                    csi_code=si.csi_code,
                    description=si.description,
                    quantity=si.quantity,
                    unit=si.unit,
                    cells=cells,
                )
            )

    return BidLevelingResponse(
        csi_division=csi_division, vendors=vendors, rows=rows
    )
