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
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings as app_settings
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
from .discipline_agent import (
    DisciplineAgentResult,
    gather_corpus_for_discipline,
    run_discipline_agent,
)
from .discipline_config import disciplines_for_project
from .gap_detector import detect_gaps
from .link_judge import judge_run as judge_links
from .project_profiler import get_or_create_profile
from .quantity_resolver import resolve_quantities
from .retriever import RetrievedChunk
from .citation_validator import (
    DEFAULT_MIN_OVERLAP,
    CitationVerdict,
    filter_valid_citations,
)
from .schedule_miner import mine_schedules
from .scope_deduper import dedupe
from .scope_verifier import verify_low_confidence
from .package_narrative import write_narratives_for_run
from .quantity_sanity import check_run as check_quantity_sanity
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
# Per-discipline mode: each discipline_agent call loads ~80K tokens of
# corpus, so 3 concurrent calls keep peak input around 240K tokens —
# safely under per-minute token rate limits.
_DISCIPLINE_CONCURRENCY = 3


async def _hydrate_chunks_by_id(chunk_ids: list[str]) -> dict:
    """Bulk-fetch Chunk rows + their Document rows for citation lookups.

    Returns {chunk_id: (chunk, doc_type)}. Missing chunk_ids are simply
    absent from the dict; callers should treat that as "skip this
    citation" rather than failing the whole emission. The agent
    sometimes fabricates chunk_ids by mis-quoting the in-context
    label; we treat those as "spec_id_X mentioned but not actually a
    valid chunk" rather than poisoning the whole item.
    """
    if not chunk_ids:
        return {}
    from sqlalchemy import select as _select

    from ..models import Chunk

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                _select(Chunk, Document.doc_type)
                .join(Document, Chunk.document_id == Document.id)
                .where(Chunk.id.in_(chunk_ids))
            )
        ).all()
    return {row[0].id: (row[0], row[1]) for row in rows}


def _candidate_from_discipline_scope_item(
    item: dict,
    chunk_lookup: dict,
) -> CandidateItem | None:
    """Convert a discipline_agent emitted scope_item into a CandidateItem.

    The discipline agent's bilateral-evidence requirement (minItems=1
    on both spec_chunk_ids and drawing_chunk_ids) is enforced at the
    Anthropic tool input_schema layer, so by the time we see the item
    here both sides are guaranteed non-empty IF the model obeyed its
    schema. We still defensively skip items that hydrate to zero
    real chunks (rare but possible if the model fabricated ids).
    """
    csi_code = (item.get("csi_code") or "").strip()
    description = (item.get("description") or "").strip()
    if not csi_code or not description:
        return None

    spec_ids = [str(x) for x in (item.get("spec_chunk_ids") or [])]
    drawing_ids = [str(x) for x in (item.get("drawing_chunk_ids") or [])]
    all_ids = spec_ids + drawing_ids

    supporting: list[RetrievedChunk] = []
    for cid in all_ids:
        hit = chunk_lookup.get(cid)
        if hit is None:
            continue
        chunk, _doc_type = hit
        supporting.append(
            RetrievedChunk(
                chunk=chunk,
                dense_score=0.0,
                sparse_score=0.0,
                rrf_score=0.0,
                rerank_score=None,
                snippet=(chunk.text[:240] if chunk.text else None),
            )
        )
    if not supporting:
        return None

    # Defensive truncation: ScopeItem column widths cap quantity at 64,
    # unit at 16, location at 255. The agent occasionally writes long
    # phrases here ("approximately 4500 SF of pavement marking on
    # parking lot"). Don't lose the row over a string overflow — keep
    # the truncated form and let the quantity_resolver normalize later.
    def _trunc(s, n):
        if not s:
            return None
        s = str(s).strip()
        return s[: n - 1] + "…" if len(s) > n else s

    cand = CandidateItem(
        csi_code=csi_code,
        description=description,
        specification=item.get("specification") or None,
        quantity=_trunc(item.get("quantity"), 64),
        unit=_trunc(item.get("unit"), 16),
        location=_trunc(item.get("location"), 255),
        extraction_method="discipline_agent_v1",
        source_chunk_ids=all_ids,
        found_by_query=_trunc(
            f"discipline_agent[{item.get('csi_code', '?')}]", 64
        ),
        supporting_chunks=supporting,
    )
    confidence = item.get("confidence")
    if isinstance(confidence, (int, float)):
        setattr(cand, "_confidence", float(confidence))
    else:
        setattr(cand, "_confidence", 0.85)
    setattr(cand, "_votes", [])
    setattr(cand, "_ground_method", "agent_emitted")
    setattr(cand, "_ground_note", None)
    return cand


_CSI_CODE_RE = re.compile(r"^\d{2} \d{2} \d{2}$")
_CSI_DIV_RE = re.compile(r"^\d{2}$")


def _canonical_csi_section(value: str | None) -> str | None:
    """Return the input only if it's a real 6-char 'NN NN NN' CSI section.

    The discipline agent sometimes returns freeform titles in the
    csi_section slot ('C500/C501 Site Details'). Those don't fit the
    Gap table's VARCHAR(16) and would be misleading anyway since they
    aren't grounded against the project's CSI taxonomy. Drop them
    rather than storing — the agent's full text already lives in the
    Gap.description, so no information is lost.
    """
    if not value:
        return None
    s = str(value).strip()
    if _CSI_CODE_RE.match(s):
        return s
    return None


def _canonical_csi_division(value: str | None) -> str | None:
    """Return the input only if it's a 2-digit CSI division code."""
    if not value:
        return None
    s = str(value).strip()
    if _CSI_DIV_RE.match(s):
        return s
    return None


async def _persist_discipline_gaps(
    run_id: str,
    project_id: str,
    discipline_results: list[DisciplineAgentResult],
) -> int:
    """Persist spec_without_drawing / drawing_without_spec / unilateral.

    These are the discipline_agent's own gap signals — separate from the
    cross-document gaps the gap_detector finds afterwards. Stamping
    them as Gap rows means HITL UI sees them in one queue.
    """
    from ..models import Gap

    n_inserted = 0
    async with SessionLocal() as db:
        for res in discipline_results:
            for entry in res.spec_without_drawing or []:
                desc = (entry.get("summary") or entry.get("description") or "").strip()
                if not desc:
                    continue
                raw_section = entry.get("csi_section")
                section = _canonical_csi_section(raw_section)
                # If the model returned a non-canonical csi_section
                # (e.g. a sheet title), preserve that text in the
                # description so reviewers can still see it.
                section_hint = (
                    f" (ref: {raw_section})"
                    if raw_section and not section
                    else ""
                )
                db.add(
                    Gap(
                        project_id=project_id,
                        run_id=run_id,
                        gap_type="unilateral_evidence",
                        csi_section=section,
                        description=(
                            f"[{res.discipline}] SPEC mandate without drawing "
                            f"evidence: {desc}{section_hint}"
                        ),
                        severity="warn",
                        suggested_remediation=(
                            "Locate the drawing detail or schedule that "
                            "implements this mandate, or note the omission "
                            "as a Design RFI."
                        ),
                    )
                )
                n_inserted += 1
            for entry in res.drawing_without_spec or []:
                desc = (entry.get("description") or "").strip()
                if not desc:
                    continue
                db.add(
                    Gap(
                        project_id=project_id,
                        run_id=run_id,
                        gap_type="unilateral_evidence",
                        description=(
                            f"[{res.discipline}] DRAWING shows item without "
                            f"spec mandate: {desc}"
                        ),
                        severity="warn",
                        suggested_remediation=(
                            "Confirm the spec division covers this item, or "
                            "raise as a Design RFI for the spec author."
                        ),
                    )
                )
                n_inserted += 1
            for entry in (res.raw or {}).get("unilateral_items") or []:
                desc = (entry.get("description") or "").strip()
                if not desc:
                    continue
                side = entry.get("evidence_side") or "?"
                csi_code = (entry.get("csi_code") or "").strip()
                db.add(
                    Gap(
                        project_id=project_id,
                        run_id=run_id,
                        gap_type="unilateral_evidence",
                        csi_division=_canonical_csi_division(csi_code[:2] if csi_code else None),
                        csi_section=_canonical_csi_section(csi_code),
                        description=(
                            f"[{res.discipline}/{side}] {desc} — "
                            f"reason: {entry.get('reason') or '(none given)'}"
                        ),
                        severity="warn",
                    )
                )
                n_inserted += 1
        await db.commit()
    return n_inserted


async def _run_per_discipline(
    project_id: str,
    profile: ProjectProfile,
    taxonomy: CSITaxonomy,
    run_id: str,
    active_division_codes: set[str],
) -> tuple[list[_DivisionResult], list[DisciplineAgentResult], float]:
    """P3 orchestration: run one discipline_agent per active discipline.

    Returns:
      - division_results: scope_items bucketed by csi_code[:2] so the
        existing _persist_results function can write them
      - discipline_results: raw agent outputs (used for gap persistence)
      - total_cost_usd
    """
    disciplines = disciplines_for_project(active_division_codes)
    if not disciplines:
        log.warning(
            "scope_runner[per-discipline]: no disciplines mapped from "
            "active divisions %s — nothing to run",
            sorted(active_division_codes),
        )
        return [], [], 0.0

    log.info(
        "scope_runner[per-discipline]: running %d disciplines: %s",
        len(disciplines), [d.key for d in disciplines],
    )

    sem = asyncio.Semaphore(_DISCIPLINE_CONCURRENCY)

    async def with_sem(d):
        async with sem:
            try:
                return await run_discipline_agent(project_id, d)
            except Exception:  # noqa: BLE001
                log.exception(
                    "scope_runner[per-discipline]: agent for %s failed",
                    d.key,
                )
                await _bump_progress(run_id, failed_inc=1)
                return None

    raw_results = await asyncio.gather(*(with_sem(d) for d in disciplines))
    discipline_results: list[DisciplineAgentResult] = [
        r for r in raw_results if r is not None
    ]
    for _ in discipline_results:
        await _bump_progress(run_id, completed_inc=1)

    # Hydrate every cited chunk_id in one go so we don't N+1 the DB
    all_chunk_ids: set[str] = set()
    for r in discipline_results:
        for item in r.scope_items:
            for cid in (item.get("spec_chunk_ids") or []):
                all_chunk_ids.add(str(cid))
            for cid in (item.get("drawing_chunk_ids") or []):
                all_chunk_ids.add(str(cid))
    chunk_lookup = await _hydrate_chunks_by_id(list(all_chunk_ids))

    # Convert + bucket by division, then dedupe within bucket
    by_division: dict[str, list[CandidateItem]] = {}
    total_emitted = 0
    for r in discipline_results:
        for item in r.scope_items:
            cand = _candidate_from_discipline_scope_item(item, chunk_lookup)
            if cand is None:
                continue
            # Ground the CSI code against the project's taxonomy now,
            # so persisted rows have a canonicalised code.
            ground = ground_code(cand.csi_code, taxonomy)
            cand.csi_code = ground.code
            setattr(cand, "_ground_method", ground.method)
            setattr(cand, "_ground_note", ground.note)
            div = cand.csi_code[:2]
            by_division.setdefault(div, []).append(cand)
            total_emitted += 1

    division_results: list[_DivisionResult] = []
    for div_code, cands in by_division.items():
        deduped = await dedupe(cands)
        division_results.append(
            _DivisionResult(
                division_code=div_code,
                accepted=deduped,
                candidates_total=len(cands),
                cost_usd=0.0,  # cost rolled up at the discipline level
            )
        )

    total_cost = sum(r.cost_usd for r in discipline_results)
    log.info(
        "scope_runner[per-discipline]: %d disciplines → %d items across "
        "%d divisions ($%.4f)",
        len(discipline_results),
        total_emitted,
        len(by_division),
        total_cost,
    )
    return division_results, discipline_results, total_cost


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
    """Write ScopeItem + ScopeCitation rows. Returns (candidates, validated, deduped).

    L2 — strict citations: every citation runs through citation_validator
    before persistence. Citations that are EMPTY / HEADER_ONLY / LOW_OVERLAP
    are dropped; the bilateral-evidence rollup downstream then reflects
    only structurally-grounded citations. Schedule-miner candidates skip
    this gate — their synthetic per-row chunks are deterministic and
    already pass the miner's hallucination guard.
    """
    candidates_total = 0
    validated_total = 0
    deduped_total = 0
    citations_kept = 0
    citations_dropped: dict[str, int] = {"EMPTY": 0, "HEADER_ONLY": 0, "LOW_OVERLAP": 0}
    items_zeroed = 0  # items that lost ALL citations to validation
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

                # Strict citations: validate every chunk before persistence.
                # Schedule-miner candidates use synthetic per-row chunks that
                # already pass the miner's hallucination guard — skip the
                # gate for those.
                if cand.extraction_method == "schedule_miner":
                    chunks_to_persist = list(cand.supporting_chunks)
                else:
                    kept, dropped = filter_valid_citations(
                        cand.description, cand.supporting_chunks
                    )
                    chunks_to_persist = kept
                    for _, verdict in dropped:
                        citations_dropped[verdict.reason] = (
                            citations_dropped.get(verdict.reason, 0) + 1
                        )
                    if not chunks_to_persist and cand.supporting_chunks:
                        items_zeroed += 1
                        log.info(
                            "citation_validator: item %s lost ALL %d citations "
                            "(reasons=%s) — desc=%r",
                            item.id, len(cand.supporting_chunks),
                            [v.reason for _, v in dropped],
                            (cand.description or "")[:80],
                        )

                # Citations
                for r in chunks_to_persist:
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
                    citations_kept += 1
        await db.commit()

    total_dropped = sum(citations_dropped.values())
    if total_dropped or items_zeroed:
        log.info(
            "citation_validator: kept=%d dropped=%d (empty=%d header=%d "
            "low_overlap=%d) items_zeroed=%d",
            citations_kept,
            total_dropped,
            citations_dropped.get("EMPTY", 0),
            citations_dropped.get("HEADER_ONLY", 0),
            citations_dropped.get("LOW_OVERLAP", 0),
            items_zeroed,
        )
    return candidates_total, validated_total, deduped_total


async def _compute_input_pdf_hash(project_id: str) -> str:
    """SHA-256 over all project_document SHA-256s in lexical order.

    Captures the project's input state: any change to a document's content
    (which would change its sha256) flips this hash. Used together with
    model_versions to make runs deterministic-input for drift detection.
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Document.sha256)
                .where(Document.project_id == project_id)
                .where(Document.source == "project_document")
                .order_by(Document.sha256)
            )
        ).scalars().all()
    h = hashlib.sha256()
    for sha in rows:
        h.update(sha.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _capture_model_versions() -> dict:
    """Snapshot every model the pipeline currently uses.

    Recorded on the run row so a future re-run with different model
    versions produces a comparable diff (model change vs. input change).
    """
    return {
        # Extraction + validation
        "extractor": "claude-sonnet-4-6",
        "validator": app_settings.classifier_model,
        "verifier": "claude-opus-4-7",
        "schedule_miner": app_settings.classifier_model,
        # Stage 3
        "link_judge": app_settings.classifier_model,
        "conflict_arbitrator": "claude-opus-4-7",
        # Phase 1/2
        "classifier": app_settings.classifier_model,
        "vision": app_settings.vision_model,
        "vision_provider": app_settings.vision_provider,
        # Phase 3 (retrieval)
        "embedding_model": app_settings.embedding_model,
        "embedding_dim": app_settings.embedding_dim,
        "rerank_model": app_settings.rerank_model,
        "contextualizer_model": app_settings.contextualizer_model,
    }


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

    # Reproducibility snapshot — pinned at run start so even if config or
    # env vars change mid-run, this row knows exactly what was used.
    pdf_hash = await _compute_input_pdf_hash(project_id)
    model_versions = _capture_model_versions()

    # Per-discipline mode counts disciplines (~10) instead of divisions
    # for sections_total so progress UI shows accurate denominator.
    mode = (app_settings.scope_orchestration_mode or "per-discipline").strip()
    if mode not in ("per-discipline", "per-division"):
        log.warning(
            "scope_runner: unknown scope_orchestration_mode=%r, "
            "falling back to per-discipline",
            mode,
        )
        mode = "per-discipline"

    active_division_codes = {d.code for d in divisions_to_process}
    if mode == "per-discipline":
        sections_total = (
            len(disciplines_for_project(active_division_codes)) or 1
        )
    else:
        sections_total = len(divisions_to_process)

    # Create the run row
    async with SessionLocal() as db:
        run = ScopeExtractionRun(
            project_id=project_id,
            status="running",
            sections_total=sections_total,
            config={
                "mode": mode,
                "divisions": [d.code for d in divisions_to_process],
                "extractor_model": "claude-sonnet-4-6",
                "validator_model": "claude-haiku-4-5",
                "queries_per_division": 3,
                "votes_per_candidate": 3,
                "dedupe_threshold": 0.92,
            },
            model_versions=model_versions,
            input_pdf_hash=pdf_hash,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    log.info(
        "scope_runner: reproducibility — input_pdf_hash=%s, model_versions=%s",
        pdf_hash[:12], list(model_versions.keys()),
    )

    log.info(
        "scope_runner: starting run %s for project %s — mode=%s, "
        "%d divisions in scope",
        run_id, project_id, mode, len(divisions_to_process),
    )

    # Stage 0 — Schedule miner pre-pass. Walks every Phase-2 ExtractedSchedule
    # and converts quantifiable rows into structured CandidateItems. Catches
    # the per-row enumerations that Sonnet's EVE tends to summarize. Runs in
    # both modes — the discipline_agent is exhaustive on schedules in
    # principle, but the deterministic miner is a safety net for any rows
    # the LLM dropped.
    t0 = time.perf_counter()
    schedule_candidates_by_division, schedule_cost = await mine_schedules(
        project_id, taxonomy
    )
    log.info(
        "scope_runner: schedule miner produced candidates in %d divisions, $%.4f",
        len(schedule_candidates_by_division),
        schedule_cost,
    )

    discipline_results: list[DisciplineAgentResult] = []
    if mode == "per-discipline":
        division_results, discipline_results, discipline_cost = (
            await _run_per_discipline(
                project_id,
                profile,
                taxonomy,
                run_id,
                active_division_codes,
            )
        )
        # Bolt on schedule-miner items whose division wasn't covered by
        # any discipline_agent's emitted items — keeps the deterministic
        # row enumeration as a backstop against the agent dropping a
        # schedule entirely.
        emitted_divisions = {dr.division_code for dr in division_results}
        for div_code, miner_cands in schedule_candidates_by_division.items():
            if not miner_cands:
                continue
            if div_code in emitted_divisions:
                # Append to the existing bucket (they'll dedupe in
                # post-process if they overlap with the agent emissions)
                bucket = next(
                    (dr for dr in division_results if dr.division_code == div_code),
                    None,
                )
                if bucket is not None:
                    for c in miner_cands:
                        ground = ground_code(c.csi_code, taxonomy)
                        c.csi_code = ground.code
                        setattr(c, "_votes", [])
                        setattr(c, "_confidence", 1.0)
                        setattr(c, "_ground_method", ground.method)
                        setattr(c, "_ground_note", ground.note)
                    bucket.accepted.extend(miner_cands)
                    bucket.candidates_total += len(miner_cands)
                continue
            # Fresh bucket: discipline_agent didn't emit for this division
            for c in miner_cands:
                ground = ground_code(c.csi_code, taxonomy)
                c.csi_code = ground.code
                setattr(c, "_votes", [])
                setattr(c, "_confidence", 1.0)
                setattr(c, "_ground_method", ground.method)
                setattr(c, "_ground_note", ground.note)
            deduped = await dedupe(miner_cands)
            division_results.append(
                _DivisionResult(
                    division_code=div_code,
                    accepted=deduped,
                    candidates_total=len(miner_cands),
                    cost_usd=0.0,
                )
            )
    else:
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
                except Exception:  # noqa: BLE001
                    log.exception("scope_runner: division %s failed", division.code)
                    await _bump_progress(run_id, failed_inc=1)
                    return _DivisionResult(division.code, [], 0, 0.0)

        division_results = await asyncio.gather(
            *(process_with_sem(d) for d in divisions_to_process)
        )
        discipline_cost = 0.0

        # Surface schedule-miner items whose division wasn't in the
        # relevance set (e.g. miner classified a schedule into Div 12
        # but trade filter skipped it). Don't drop them silently —
        # they're valuable line items.
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
                    cost_usd=0.0,
                )
            )

    total_latency_ms = int((time.perf_counter() - t0) * 1000)

    candidates, validated, deduped = await _persist_results(
        run_id, project_id, division_results, taxonomy
    )

    # Per-discipline mode also persists the agent's spec-without-drawing /
    # drawing-without-spec / unilateral_items as Gap rows so HITL sees them.
    if mode == "per-discipline" and discipline_results:
        n_agent_gaps = await _persist_discipline_gaps(
            run_id, project_id, discipline_results
        )
        log.info(
            "scope_runner: per-discipline gaps persisted = %d", n_agent_gaps,
        )

    # Stage E — Quantity Resolver. Deterministic post-pass: aggregate every
    # quantity signal (Sonnet-stated, schedule-miner cluster sizes, regex-
    # sniffed numerics from excerpts) and resolve a final qty + confidence
    # band per ScopeItem. No LLM calls.
    qty_updated = await resolve_quantities(run_id)
    log.info("scope_runner: quantity resolver updated %d items", qty_updated)

    # Stage E2 — Quantity sanity heuristics (P7, W7 mitigation).
    # Pure rules over qty_value + qty_uom + csi_division. Flags items
    # whose number is implausible for the unit/division pair (decimal
    # shifts, unit-category mismatches). Persists as Gap rows of type
    # 'qty_implausible'. No LLM cost.
    try:
        sanity_stats = await check_quantity_sanity(run_id)
        log.info(
            "scope_runner: quantity sanity — %d findings (%d blocker, %d warn) "
            "across %d items",
            sanity_stats.get("findings", 0),
            sanity_stats.get("blockers", 0),
            sanity_stats.get("warnings", 0),
            sanity_stats.get("checked", 0),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("scope_runner: quantity sanity check failed: %s", e)

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

    # Stage G2 — Per-package narrative writer (P6). One Haiku call per
    # TradePackage drafts a Markdown bid-invitation cover letter that the
    # GC can copy into an email to subs. Persists to TradePackage.narrative_md.
    # Idempotent: re-running rewrites every package's narrative.
    try:
        n_narratives, narrative_cost = await write_narratives_for_run(run_id)
        log.info(
            "scope_runner: package narratives — %d drafted ($%.4f)",
            n_narratives, narrative_cost,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("scope_runner: package narrative writer failed: %s", e)
        narrative_cost = 0.0

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
        + discipline_cost
        + schedule_cost
        + verifier_cost
        + link_judge_cost
        + arbitration_cost
        + narrative_cost
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
