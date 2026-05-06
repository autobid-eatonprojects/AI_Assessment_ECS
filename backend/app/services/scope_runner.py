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
    Document,
    ProjectProfile,
    ScopeCitation,
    ScopeExtractionRun,
    ScopeItem,
    TradeDivisionRelevance,
)
from .bilateral_evidence import classify_run as classify_bilateral
from .conflict_arbitrator import arbitrate_run as arbitrate_conflicts
from .conflict_detector import detect_run as detect_conflicts
from .csi_grounder import ground_code
from .gap_detector import detect_gaps
from .link_judge import judge_run as judge_links
from .project_profiler import get_or_create_profile
from .quantity_resolver import resolve_quantities
from .schedule_miner import mine_schedules
from .scope_deduper import dedupe
from .scope_verifier import verify_low_confidence
from .trade_bundler import bundle_run as bundle_packages
from .trust_score import compute_run as compute_trust_score
from .scope_extractor import (
    CandidateItem,
    ScopeExtractorUnavailable,
    extract_division_candidates,
)
from .scope_validator import ValidationResult, validate_candidate
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
# while still finishing a typical mid-size project (~20-30 relevant divisions)
# in roughly 10-15 minutes.
_DIVISION_CONCURRENCY = 4
_VALIDATE_CONCURRENCY = 10


async def _process_division(
    project_id: str,
    division: CSIDivision,
    profile: ProjectProfile,
    taxonomy: CSITaxonomy,
    run_id: str,
    validator_sem: asyncio.Semaphore,
    schedule_candidates: list[CandidateItem],
) -> _DivisionResult:
    """Run Stages A–D for one division. Persists nothing; orchestrator does that.

    `schedule_candidates` are the (already-deterministic) Stage 0 schedule-miner
    items for this division. They bypass Stage B validation because they
    enumerate Phase-2 structured rows rather than generating from prose.
    """
    log.info(
        "scope_runner: division %s starting (schedule_miner=%d)",
        division.code,
        len(schedule_candidates),
    )
    eve_candidates, _, extract_cost = await extract_division_candidates(
        project_id, division, profile, taxonomy
    )

    candidates = list(eve_candidates) + list(schedule_candidates)

    if not candidates:
        log.info("scope_runner: division %s — no candidates", division.code)
        await _bump_progress(run_id, completed_inc=1)
        return _DivisionResult(division.code, [], 0, extract_cost)

    # Stage B — 3-vote validation (parallel within division). Schedule-miner
    # candidates skip validation: they came from deterministic Phase-2 data,
    # not a generative summarizer, so the hallucination check doesn't apply.
    async def validate_one(c: CandidateItem):
        if c.extraction_method == "schedule_miner":
            return c, ValidationResult(accept=True, confidence=1.0, votes=[]), 0.0
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


def _evidence_type_for_doc_type(doc_type: str | None) -> str:
    """Map a Document.doc_type to a ScopeCitation.evidence_type bucket.

    Used so the bilateral-evidence rollup (Stage 2) can be a single GROUP BY
    on scope_citations rather than a JOIN through chunks → documents.
    """
    if doc_type == "drawing-set":
        return "drawing"
    if doc_type == "written-spec":
        return "spec"
    if doc_type in ("bid-quote", "scope-letter"):
        return "bid"
    return "other"


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
        # Pre-load doc_type for every chunk's source document so each
        # citation insert can stamp evidence_type without an N+1 lookup.
        chunk_doc_ids: set[str] = set()
        for d in division_results:
            for cand in d.accepted:
                for r in cand.supporting_chunks:
                    if r.chunk.document_id:
                        chunk_doc_ids.add(r.chunk.document_id)
        doc_type_by_id: dict[str, str | None] = {}
        if chunk_doc_ids:
            doc_rows = await db.execute(
                select(Document.id, Document.doc_type).where(
                    Document.id.in_(chunk_doc_ids)
                )
            )
            doc_type_by_id = {row[0]: row[1] for row in doc_rows.all()}

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
                    src_doc_type = doc_type_by_id.get(chunk.document_id) if chunk.document_id else None
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
                            evidence_type=_evidence_type_for_doc_type(src_doc_type),
                            doc_type_at_capture=src_doc_type,
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

    # Stage 0 — Schedule miner pre-pass. Walks every Phase-2 ExtractedSchedule
    # and converts quantifiable rows into structured CandidateItems. Catches
    # the per-row enumerations that Sonnet's EVE tends to summarize.
    t0 = time.perf_counter()
    schedule_candidates_by_division, schedule_cost = await mine_schedules(
        project_id, taxonomy
    )
    log.info(
        "scope_runner: schedule miner produced candidates in %d divisions, $%.4f",
        len(schedule_candidates_by_division),
        schedule_cost,
    )

    sem_div = asyncio.Semaphore(_DIVISION_CONCURRENCY)
    sem_validate = asyncio.Semaphore(_VALIDATE_CONCURRENCY)

    async def process_with_sem(division: CSIDivision):
        async with sem_div:
            try:
                return await _process_division(
                    project_id,
                    division,
                    profile,
                    taxonomy,
                    run_id,
                    sem_validate,
                    schedule_candidates_by_division.get(division.code, []),
                )
            except Exception as e:  # noqa: BLE001
                log.exception("scope_runner: division %s failed", division.code)
                await _bump_progress(run_id, failed_inc=1)
                return _DivisionResult(division.code, [], 0, 0.0)

    division_results = await asyncio.gather(
        *(process_with_sem(d) for d in divisions_to_process)
    )

    # Surface schedule-miner items whose division wasn't in the relevance set
    # (e.g. miner classified a schedule into Div 12 but trade filter skipped it).
    # Don't drop them silently — they're valuable line items.
    relevant_codes = {d.code for d in divisions_to_process}
    orphan_divisions = set(schedule_candidates_by_division) - relevant_codes
    for div_code in orphan_divisions:
        orphan_cands = schedule_candidates_by_division[div_code]
        if not orphan_cands:
            continue
        log.info(
            "scope_runner: surfacing %d schedule-miner items in non-relevant div %s",
            len(orphan_cands),
            div_code,
        )
        # Run them through CSI grounding + dedupe directly (no validation needed)
        for c in orphan_cands:
            ground = ground_code(c.csi_code, taxonomy)
            c.csi_code = ground.code
            setattr(c, "_votes", [])
            setattr(c, "_confidence", 1.0)
            setattr(c, "_ground_method", ground.method)
            setattr(c, "_ground_note", ground.note)
        deduped_orphans = await dedupe(orphan_cands)
        division_results.append(
            _DivisionResult(
                division_code=div_code,
                accepted=deduped_orphans,
                candidates_total=len(orphan_cands),
                cost_usd=0.0,  # schedule_cost is already counted
            )
        )

    total_latency_ms = int((time.perf_counter() - t0) * 1000)

    candidates, validated, deduped = await _persist_results(
        run_id, project_id, division_results, taxonomy
    )

    # Stage E — Quantity Resolver. Deterministic post-pass: aggregate every
    # quantity signal (Sonnet-stated, schedule-miner cluster sizes, regex-
    # sniffed numerics from excerpts) and resolve a final qty + confidence
    # band per ScopeItem. No LLM calls.
    qty_updated = await resolve_quantities(run_id)
    log.info("scope_runner: quantity resolver updated %d items", qty_updated)

    # Stage F — Opus 4.7 reflection pass on flagged items only (red
    # validator confidence + qty conflicts). Catches misses where the
    # primary 3-vote validator wasn't decisive. ~$1-2 on a typical run.
    kept, revised, rejected, verifier_cost = await verify_low_confidence(run_id)
    log.info(
        "scope_runner: verifier — %d kept, %d revised, %d rejected, $%.3f",
        kept,
        revised,
        rejected,
        verifier_cost,
    )

    # Stage F2 — Link judge (per-citation entailment via Haiku 4.5).
    # First pass that operates on the actual cited chunks rather than
    # retrieved candidates. Catches "good item, wrong citation" cases.
    judged_count, pass_count, link_judge_cost = await judge_links(run_id)
    log.info(
        "scope_runner: link judge — %d/%d citations passed (%.0f%%), $%.3f",
        pass_count,
        judged_count,
        (pass_count / judged_count * 100) if judged_count else 0,
        link_judge_cost,
    )

    # Stage F3 — Conflict detection. Pure embedding clustering; no LLM cost.
    # Surfaces within-CSI qty/unit disagreements + cross-division overlaps.
    conflict_rollup = await detect_conflicts(run_id)
    log.info(
        "scope_runner: conflicts — qty=%d unit=%d cross_div=%d",
        conflict_rollup.qty_mismatch,
        conflict_rollup.unit_mismatch,
        conflict_rollup.cross_division,
    )

    # Stage F4 — Conflict auto-arbitration (Opus 4.7). Picks winners for
    # high-confidence clusters; ambiguous cases stay status='open' for HITL.
    arbitrated, deferred, arbitration_cost = await arbitrate_conflicts(run_id)
    log.info(
        "scope_runner: arbitrated %d / deferred %d to HITL, $%.3f",
        arbitrated,
        deferred,
        arbitration_cost,
    )

    # Stage F5 — Bilateral evidence + tier classification. Now that link
    # judge has populated is_link_judge_pass, the EXPLICITLY_CITED tier
    # check is fully effective.
    tier_counts = await classify_bilateral(run_id)
    log.info("scope_runner: bilateral evidence — %s", tier_counts)

    # Stage G — Trade bundling. AGC-style rollup of items into bid packages,
    # driven by app/data/bundling_rules.yaml + per-project overrides. Pure
    # SQL. Idempotent (re-runnable).
    bundle_stats = await bundle_packages(run_id)
    log.info(
        "scope_runner: bundling — %d packages, %d items, %d unbundled",
        bundle_stats.packages_created,
        bundle_stats.items_assigned,
        bundle_stats.unbundled_items,
    )

    # Stage H — Gap detection. Surfaces missing divisions/sections,
    # one-sided items, and unresolved cross-references as Gap rows for
    # the HITL queue. No LLM calls.
    gap_stats = await detect_gaps(run_id)
    log.info(
        "scope_runner: gaps — missing_div=%d missing_sec=%d "
        "unilateral=%d cross_ref=%d",
        gap_stats.missing_division,
        gap_stats.missing_section,
        gap_stats.unilateral_evidence,
        gap_stats.unresolved_cross_reference,
    )

    # Stage I — Trust score. 4-component weighted score persisted on the
    # run row + mirrored to Project.trust_score_latest. No LLM calls.
    trust = await compute_trust_score(run_id)
    log.info(
        "scope_runner: trust score = %.3f (%s)", trust.score, trust.tier
    )

    # Compute final cost from llm_calls for this run window
    total_cost = (
        sum(d.cost_usd for d in division_results)
        + schedule_cost
        + verifier_cost
        + link_judge_cost
        + arbitration_cost
    )

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
