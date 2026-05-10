"""Phase 8 endpoints — Bid Analysis."""

from __future__ import annotations

import asyncio as _asyncio
from collections import Counter

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..models import (
    BidCoverage,
    BidExclusion,
    BidExtractionRun,
    BidInclusion,
    BidLineItem,
    BidSummary,
    Project,
    ScopeExtractionRun,
    ScopeItem,
)
from ..schemas import (
    BidAnalysisOverview,
    BidCoverageOut,
    BidDetailOut,
    BidExclusionOut,
    BidInclusionOut,
    BidLineItemOut,
    BidRunOut,
    BidSummaryOut,
    ScopeItemOut,
)
from ..services.bid_analysis_runner import (
    BidAnalysisUnavailable,
    schedule_bid_analysis,
)
from ..services.bid_extractor import list_priced_bids
from .deps import DB, CurrentUser

router = APIRouter(
    prefix="/projects/{project_id}/bid-analysis", tags=["bid-analysis"]
)


async def _ensure_project(db, project_id: str) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    proj = result.scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return proj


@router.get("", response_model=BidAnalysisOverview)
async def get_overview(
    project_id: str, db: DB, _: CurrentUser
) -> BidAnalysisOverview:
    await _ensure_project(db, project_id)

    run_result = await db.execute(
        select(BidExtractionRun)
        .where(BidExtractionRun.project_id == project_id)
        .order_by(BidExtractionRun.started_at.desc())
        .limit(1)
    )
    latest_run = run_result.scalar_one_or_none()

    if latest_run is None:
        return BidAnalysisOverview(
            project_id=project_id,
            latest_run=None,
            bid_summaries=[],
            coverage_counts={
                "covered": 0,
                "partial": 0,
                "excluded": 0,
                "not_covered": 0,
            },
        )

    summaries = (
        await db.execute(
            select(BidSummary)
            .where(BidSummary.project_id == project_id)
            .where(BidSummary.run_id == latest_run.id)
        )
    ).scalars().all()

    coverage_rows = (
        await db.execute(
            select(BidCoverage.status)
            .where(BidCoverage.run_id == latest_run.id)
        )
    ).all()
    counts: Counter[str] = Counter(row[0] for row in coverage_rows)

    return BidAnalysisOverview(
        project_id=project_id,
        latest_run=BidRunOut.model_validate(latest_run),
        bid_summaries=[BidSummaryOut.model_validate(s) for s in summaries],
        coverage_counts={
            "covered": counts.get("covered", 0),
            "partial": counts.get("partial", 0),
            "excluded": counts.get("excluded", 0),
            "not_covered": counts.get("not_covered", 0),
        },
    )


@router.post(
    "/runs", response_model=BidRunOut, status_code=status.HTTP_202_ACCEPTED
)
async def start_run(project_id: str, db: DB, _: CurrentUser) -> BidRunOut:
    await _ensure_project(db, project_id)

    bids = await list_priced_bids(project_id)
    if not bids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "no priced bids uploaded — upload at least one bid-quote or "
                "scope-letter document and wait for it to finish indexing"
            ),
        )

    scope = (
        await db.execute(
            select(ScopeExtractionRun)
            .where(ScopeExtractionRun.project_id == project_id)
            .where(ScopeExtractionRun.status == "complete")
            .order_by(ScopeExtractionRun.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if scope is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "no completed scope run — generate scope of work (Phase 4.3) "
                "before running bid analysis"
            ),
        )

    schedule_bid_analysis(project_id)
    for _ in range(20):
        await _asyncio.sleep(0.1)
        latest = (
            await db.execute(
                select(BidExtractionRun)
                .where(BidExtractionRun.project_id == project_id)
                .order_by(BidExtractionRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None and latest.status == "running":
            return BidRunOut.model_validate(latest)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="bid run was scheduled but never appeared in the database",
    )


@router.get("/runs/{run_id}", response_model=BidRunOut)
async def get_run(
    project_id: str, run_id: str, db: DB, _: CurrentUser
) -> BidRunOut:
    await _ensure_project(db, project_id)
    run = (
        await db.execute(
            select(BidExtractionRun).where(
                BidExtractionRun.id == run_id,
                BidExtractionRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bid run not found"
        )
    return BidRunOut.model_validate(run)


@router.get("/coverage", response_model=list[BidCoverageOut])
async def list_coverage(
    project_id: str, db: DB, _: CurrentUser
) -> list[BidCoverageOut]:
    """Return the full coverage matrix for the latest run."""
    await _ensure_project(db, project_id)
    latest = (
        await db.execute(
            select(BidExtractionRun)
            .where(BidExtractionRun.project_id == project_id)
            .order_by(BidExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return []
    rows = (
        await db.execute(
            select(BidCoverage).where(BidCoverage.run_id == latest.id)
        )
    ).scalars().all()
    return [BidCoverageOut.model_validate(r) for r in rows]


@router.get("/gaps", response_model=list[ScopeItemOut])
async def list_gaps(
    project_id: str, db: DB, _: CurrentUser
) -> list[ScopeItemOut]:
    """Scope items that no bid covers — the "missing" list for an estimator."""
    from sqlalchemy.orm import selectinload

    await _ensure_project(db, project_id)
    latest = (
        await db.execute(
            select(BidExtractionRun)
            .where(BidExtractionRun.project_id == project_id)
            .order_by(BidExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None or latest.scope_run_id is None:
        return []

    # Scope item ids that have at least one 'covered' or 'partial' BidCoverage
    covered_ids = {
        r[0]
        for r in (
            await db.execute(
                select(BidCoverage.scope_item_id)
                .where(BidCoverage.run_id == latest.id)
                .where(BidCoverage.status.in_(["covered", "partial"]))
            )
        ).all()
    }

    # All scope items from the anchored scope run, minus covered ones
    items = (
        await db.execute(
            select(ScopeItem)
            .options(selectinload(ScopeItem.citations))
            .where(ScopeItem.run_id == latest.scope_run_id)
            .order_by(ScopeItem.csi_code)
        )
    ).scalars().all()

    return [
        ScopeItemOut.model_validate(it) for it in items if it.id not in covered_ids
    ]


@router.get("/bids/{bid_document_id}", response_model=BidDetailOut)
async def get_bid_detail(
    project_id: str, bid_document_id: str, db: DB, _: CurrentUser
) -> BidDetailOut:
    """Per-bid drill-down for the bid analysis page."""
    await _ensure_project(db, project_id)
    latest = (
        await db.execute(
            select(BidExtractionRun)
            .where(BidExtractionRun.project_id == project_id)
            .order_by(BidExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no bid runs yet"
        )

    summary = (
        await db.execute(
            select(BidSummary)
            .where(BidSummary.run_id == latest.id)
            .where(BidSummary.bid_document_id == bid_document_id)
        )
    ).scalar_one_or_none()
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bid summary not found"
        )
    line_items = (
        await db.execute(
            select(BidLineItem)
            .where(BidLineItem.run_id == latest.id)
            .where(BidLineItem.bid_document_id == bid_document_id)
        )
    ).scalars().all()
    incs = (
        await db.execute(
            select(BidInclusion)
            .where(BidInclusion.run_id == latest.id)
            .where(BidInclusion.bid_document_id == bid_document_id)
        )
    ).scalars().all()
    excs = (
        await db.execute(
            select(BidExclusion)
            .where(BidExclusion.run_id == latest.id)
            .where(BidExclusion.bid_document_id == bid_document_id)
        )
    ).scalars().all()
    return BidDetailOut(
        summary=BidSummaryOut.model_validate(summary),
        line_items=[BidLineItemOut.model_validate(x) for x in line_items],
        inclusions=[BidInclusionOut.model_validate(x) for x in incs],
        exclusions=[BidExclusionOut.model_validate(x) for x in excs],
    )
