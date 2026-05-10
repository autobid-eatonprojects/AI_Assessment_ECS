"""Phase 8 orchestrator — extract bids + score the coverage matrix.

End-to-end flow per "Run bid analysis" click:
  1. Pre-conditions: at least one priced bid uploaded; a completed scope run.
  2. For every priced bid (parallel): Sonnet 4.6 extract → persist line items
     + inclusions + exclusions + summary.
  3. Build per-bid context blocks + filter (scope, bid) pairs by CSI division.
  4. Haiku coverage call per surviving pair (parallel, capped).
  5. Mark the run complete with totals + cost.

Resumability: each run creates a BidExtractionRun row that the UI polls.
We update bids_extracted + coverage_pairs_completed live as work finishes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, update

from ..database import SessionLocal
from ..models import (
    BidExtractionRun,
    Document,
    ScopeExtractionRun,
)
from .bid_coverage import score_coverage
from .bid_extractor import extract_all_bids, list_priced_bids

log = logging.getLogger(__name__)


class BidAnalysisUnavailable(Exception):
    """Pre-conditions not met."""


async def run_bid_analysis(project_id: str) -> BidExtractionRun:
    """Orchestrate Phase 8 end-to-end. Persists a BidExtractionRun row."""
    bids = await list_priced_bids(project_id)
    if not bids:
        raise BidAnalysisUnavailable(
            "no priced bids uploaded — upload bid quotes / scope letters first"
        )

    # Find the most recent completed scope run; we anchor coverage to it
    async with SessionLocal() as db:
        scope_run = (
            await db.execute(
                select(ScopeExtractionRun)
                .where(ScopeExtractionRun.project_id == project_id)
                .where(ScopeExtractionRun.status == "complete")
                .order_by(ScopeExtractionRun.completed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if scope_run is None:
            raise BidAnalysisUnavailable(
                "no completed scope run — generate scope of work first"
            )

        run = BidExtractionRun(
            project_id=project_id,
            scope_run_id=scope_run.id,
            status="running",
            bids_total=len(bids),
            config={
                "bid_extractor_model": "claude-sonnet-4-6",
                "coverage_judge_model": "claude-haiku-4-5",
            },
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    log.info(
        "bid_analysis: starting run %s — %d bids, scope_run=%s",
        run_id,
        len(bids),
        scope_run.id,
    )

    t0 = time.perf_counter()

    # Stage 1 — extract every bid (parallel inside extract_all_bids)
    summaries = await extract_all_bids(project_id, run_id)

    async with SessionLocal() as db:
        await db.execute(
            update(BidExtractionRun)
            .where(BidExtractionRun.id == run_id)
            .values(bids_extracted=len(summaries))
        )
        await db.commit()

    # Stage 2 — score the coverage matrix
    async def progress_cb(n_completed: int):
        async with SessionLocal() as db:
            await db.execute(
                update(BidExtractionRun)
                .where(BidExtractionRun.id == run_id)
                .values(coverage_pairs_completed=n_completed)
            )
            await db.commit()

    pair_total, coverage_cost = await score_coverage(project_id, run_id, progress_cb)

    total_latency_ms = int((time.perf_counter() - t0) * 1000)

    # Sum all costs (Sonnet bid extracts + Haiku coverage)
    extract_cost = sum((s.extraction_cost_usd or 0.0) for s in summaries)
    total_cost = extract_cost + coverage_cost

    async with SessionLocal() as db:
        await db.execute(
            update(BidExtractionRun)
            .where(BidExtractionRun.id == run_id)
            .values(
                status="complete",
                completed_at=datetime.now(timezone.utc),
                bids_extracted=len(summaries),
                coverage_pairs_total=pair_total,
                coverage_pairs_completed=pair_total,
                total_cost_usd=total_cost,
                total_latency_ms=total_latency_ms,
            )
        )
        await db.commit()

    log.info(
        "bid_analysis: run %s complete — %d bids, %d coverage pairs, $%.3f, %dms",
        run_id,
        len(summaries),
        pair_total,
        total_cost,
        total_latency_ms,
    )

    async with SessionLocal() as db:
        return await db.get(BidExtractionRun, run_id)


# Fire-and-forget scheduler — same pattern as scope_runner
_inflight: set[asyncio.Task] = set()


def schedule_bid_analysis(project_id: str) -> asyncio.Task:
    task = asyncio.create_task(_run_with_failure_capture(project_id))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return task


async def _run_with_failure_capture(project_id: str) -> None:
    try:
        await run_bid_analysis(project_id)
    except BidAnalysisUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("bid_analysis: unexpected failure for project %s", project_id)
        async with SessionLocal() as db:
            await db.execute(
                update(BidExtractionRun)
                .where(BidExtractionRun.project_id == project_id)
                .where(BidExtractionRun.status == "running")
                .values(status="failed", error=str(e))
            )
            await db.commit()
