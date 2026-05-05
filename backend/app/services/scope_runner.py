"""Phase 4.3 orchestrator — run the full EVE pipeline for a project.

End-to-end flow:
    1. Verify pre-conditions (profile, relevance, taxonomy).
    2. For each relevant CSI division (with concurrency cap):
       - Stage A — multi-query extraction (3 perspectives).
       - Stage B — 3-vote majority validation per candidate.
       - Stage C — strict CSI grounding against taxonomy.
       - Stage D — embedding-based dedupe within division.
    3. Persist ScopeItem + ScopeCitation rows under a ScopeExtractionRun.

Concurrency:
    Per-division calls are dispatched in parallel (sem_division=4) — the
    Sonnet extractor is already 3 parallel sub-queries internally, so 4*3=12
    concurrent Sonnet calls is right around the rate-limit sweet spot.
    Validators run inside their own semaphore (sem_validate=10).

Resumability:
    Each run creates a ScopeExtractionRun row tracking sections_total,
    sections_completed, sections_failed in real time. The UI polls this so
    progress is visible during the ~10 minute end-to-end run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..database import SessionLocal
from ..models import (
    ProjectProfile,
    ScopeCitation,
    ScopeExtractionRun,
    ScopeItem,
    TradeDivisionRelevance,
)
from .csi_grounder import ground_code
from .project_profiler import get_or_create_profile
from .scope_deduper import dedupe
from .scope_extractor import (
    CandidateItem,
    ScopeExtractorUnavailable,
    extract_division_candidates,
)
from .scope_validator import validate_candidate
from .trade_list_parser import (
    CSIDivision,
    CSITaxonomy,
    get_taxonomy_for_project,
)

log = logging.getLogger(__name__)


@dataclass
class _DivisionResult:
    division_code: str
    accepted: list[CandidateItem]
    candidates_total: int
    cost_usd: float


class ScopeRunnerUnavailable(Exception):
    """Pre-conditions not met (no API key / no Trade_List / no profile)."""


# Concurrency caps — tuned to keep within Anthropic's per-account limits
# while still finishing the full Elks run in <15 minutes.
_DIVISION_CONCURRENCY = 4
_VALIDATE_CONCURRENCY = 10


async def _process_division(
    project_id: str,
    division: CSIDivision,
    profile: ProjectProfile,
    taxonomy: CSITaxonomy,
    run_id: str,
    validator_sem: asyncio.Semaphore,
) -> _DivisionResult:
    """Run Stages A–D for one division. Persists nothing; orchestrator does that."""
    log.info("scope_runner: division %s starting", division.code)
    candidates, _, extract_cost = await extract_division_candidates(
        project_id, division, profile, taxonomy
    )

    if not candidates:
        log.info("scope_runner: division %s — no candidates", division.code)
        await _bump_progress(run_id, completed_inc=1)
        return _DivisionResult(division.code, [], 0, extract_cost)

    # Stage B — 3-vote validation (parallel within division)
    async def validate_one(c: CandidateItem):
        result, vcost = await validate_candidate(c, project_id, validator_sem)
        return c, result, vcost

    validated_results = await asyncio.gather(
        *(validate_one(c) for c in candidates),
        return_exceptions=True,
    )

    accepted: list[CandidateItem] = []
    validate_cost = 0.0
    for r in validated_results:
        if isinstance(r, Exception):
            log.warning("scope_runner: validation error in %s: %s", division.code, r)
            continue
        c, result, cost = r
        validate_cost += cost
        if not result.accept:
            continue
        # Stage C — CSI grounding
        ground = ground_code(c.csi_code, taxonomy)
        c.csi_code = ground.code
        # Attach validation provenance
        c.specification = c.specification  # placeholder for clarity
        c_with_audit = c
        c_with_audit.supporting_chunks = c.supporting_chunks
        # Stash votes + grounding note as JSON via a dynamic attr on the
        # candidate (not part of the dataclass — runner reads later).
        setattr(c_with_audit, "_votes", result.votes)
        setattr(c_with_audit, "_confidence", result.confidence)
        setattr(c_with_audit, "_ground_method", ground.method)
        setattr(c_with_audit, "_ground_note", ground.note)
        accepted.append(c_with_audit)

    # Stage D — dedupe within division
    deduped = await dedupe(accepted)

    log.info(
        "scope_runner: division %s — %d candidates → %d validated → %d unique, $%.4f",
        division.code,
        len(candidates),
        len(accepted),
        len(deduped),
        extract_cost + validate_cost,
    )
    await _bump_progress(run_id, completed_inc=1)
    return _DivisionResult(
        division_code=division.code,
        accepted=deduped,
        candidates_total=len(candidates),
        cost_usd=extract_cost + validate_cost,
    )


async def _bump_progress(
    run_id: str,
    *,
    completed_inc: int = 0,
    failed_inc: int = 0,
) -> None:
    async with SessionLocal() as db:
        run = await db.get(ScopeExtractionRun, run_id)
        if run is None:
            return
        run.sections_completed = run.sections_completed + completed_inc
        run.sections_failed = run.sections_failed + failed_inc
        await db.commit()


async def _persist_results(
    run_id: str,
    project_id: str,
    division_results: list[_DivisionResult],
    taxonomy: CSITaxonomy,
) -> tuple[int, int, int]:
    """Write ScopeItem + ScopeCitation rows. Returns (candidates, validated, deduped)."""
    candidates_total = 0
    validated_total = 0
    deduped_total = 0
    async with SessionLocal() as db:
        for d in division_results:
            candidates_total += d.candidates_total
            validated_total += len(d.accepted)
            deduped_total += len(d.accepted)
            for cand in d.accepted:
                section = taxonomy.get_section(cand.csi_code)
                section_title = section.title if section else None
                division_label = (
                    taxonomy.get_division(cand.csi_code[:2]).label
                    if taxonomy.get_division(cand.csi_code[:2])
                    else f"{cand.csi_code[:2]} - (untitled)"
                )
                item = ScopeItem(
                    project_id=project_id,
                    run_id=run_id,
                    csi_code=cand.csi_code,
                    csi_division=cand.csi_code[:2],
                    division_label=division_label,
                    section_title=section_title,
                    description=cand.description,
                    specification=cand.specification,
                    quantity=cand.quantity,
                    unit=cand.unit,
                    location=cand.location,
                    confidence=getattr(cand, "_confidence", 1.0),
                    extraction_method=cand.extraction_method,
                    raw_extractions=[
                        {
                            "found_by_query": cand.found_by_query,
                            "csi_code_proposed": cand.csi_code,
                            "ground_method": getattr(cand, "_ground_method", "exact"),
                            "ground_note": getattr(cand, "_ground_note", None),
                        }
                    ],
                    validation_votes=getattr(cand, "_votes", None),
                )
                db.add(item)
                await db.flush()
                # Citations
                for r in cand.supporting_chunks:
                    chunk = r.chunk
                    extra = chunk.extra or {}
                    db.add(
                        ScopeCitation(
                            scope_item_id=item.id,
                            chunk_id=chunk.id,
                            document_id=chunk.document_id,
                            page_number=chunk.page_number,
                            sheet_number=extra.get("sheet_number"),
                            bbox=chunk.bbox,
                            rerank_score=r.rerank_score,
                            extraction_query=cand.found_by_query,
                            excerpt=(chunk.text[:240] if chunk.text else None),
                        )
                    )
        await db.commit()
    return candidates_total, validated_total, deduped_total


async def run_scope_extraction(project_id: str) -> ScopeExtractionRun:
    """Orchestrate Phase 4.3 end-to-end. Persists a ScopeExtractionRun row."""
    # Pre-conditions
    taxonomy = await get_taxonomy_for_project(project_id)
    if taxonomy is None:
        raise ScopeRunnerUnavailable(
            "no Trade_List.xlsx uploaded — upload one as a project document first"
        )
    try:
        profile = await get_or_create_profile(project_id)
    except ScopeExtractorUnavailable as e:
        raise ScopeRunnerUnavailable(str(e)) from e

    async with SessionLocal() as db:
        rel_result = await db.execute(
            select(TradeDivisionRelevance).where(
                TradeDivisionRelevance.project_id == project_id
            )
        )
        relevance_rows = list(rel_result.scalars().all())

    if not relevance_rows:
        raise ScopeRunnerUnavailable(
            "trade relevance not yet computed — run Phase 4.2 first"
        )

    # Resolve which divisions to process
    divisions_to_process: list[CSIDivision] = []
    for r in relevance_rows:
        if not r.effective_relevance:
            continue
        d = taxonomy.get_division(r.csi_division)
        if d is None:
            continue
        divisions_to_process.append(d)

    if not divisions_to_process:
        raise ScopeRunnerUnavailable("no relevant divisions to process")

    # Create the run row
    async with SessionLocal() as db:
        run = ScopeExtractionRun(
            project_id=project_id,
            status="running",
            sections_total=len(divisions_to_process),
            config={
                "divisions": [d.code for d in divisions_to_process],
                "extractor_model": "claude-sonnet-4-6",
                "validator_model": "claude-haiku-4-5",
                "queries_per_division": 3,
                "votes_per_candidate": 3,
                "dedupe_threshold": 0.92,
            },
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    log.info(
        "scope_runner: starting run %s for project %s — %d divisions",
        run_id,
        project_id,
        len(divisions_to_process),
    )

    sem_div = asyncio.Semaphore(_DIVISION_CONCURRENCY)
    sem_validate = asyncio.Semaphore(_VALIDATE_CONCURRENCY)

    async def process_with_sem(division: CSIDivision):
        async with sem_div:
            try:
                return await _process_division(
                    project_id, division, profile, taxonomy, run_id, sem_validate
                )
            except Exception as e:  # noqa: BLE001
                log.exception("scope_runner: division %s failed", division.code)
                await _bump_progress(run_id, failed_inc=1)
                return _DivisionResult(division.code, [], 0, 0.0)

    t0 = time.perf_counter()
    division_results = await asyncio.gather(
        *(process_with_sem(d) for d in divisions_to_process)
    )
    total_latency_ms = int((time.perf_counter() - t0) * 1000)

    candidates, validated, deduped = await _persist_results(
        run_id, project_id, division_results, taxonomy
    )

    # Compute final cost from llm_calls for this run window
    total_cost = sum(d.cost_usd for d in division_results)

    async with SessionLocal() as db:
        run = await db.get(ScopeExtractionRun, run_id)
        assert run is not None
        run.status = "complete"
        run.candidates_generated = candidates
        run.items_validated = validated
        run.items_after_dedupe = deduped
        run.total_cost_usd = total_cost
        run.total_latency_ms = total_latency_ms
        from datetime import datetime, timezone

        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)

    log.info(
        "scope_runner: run %s complete — %d items in %dms, $%.3f",
        run_id,
        deduped,
        total_latency_ms,
        total_cost,
    )
    return run


# Fire-and-forget scheduling — same pattern as Phase 1/2 processor
_inflight: set[asyncio.Task] = set()


def schedule_scope_run(project_id: str) -> asyncio.Task:
    task = asyncio.create_task(_run_with_failure_capture(project_id))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return task


async def _run_with_failure_capture(project_id: str) -> None:
    try:
        await run_scope_extraction(project_id)
    except ScopeRunnerUnavailable:
        # Pre-condition errors are surfaced to the API layer immediately;
        # nothing to do for in-flight scheduling.
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("scope_runner: unexpected failure for project %s", project_id)
        # Mark the most recent running run as failed
        async with SessionLocal() as db:
            from sqlalchemy import update

            await db.execute(
                update(ScopeExtractionRun)
                .where(ScopeExtractionRun.project_id == project_id)
                .where(ScopeExtractionRun.status == "running")
                .values(status="failed", error=str(e))
            )
            await db.commit()
