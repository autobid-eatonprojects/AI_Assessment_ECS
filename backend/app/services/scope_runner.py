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
    Chunk,
    Document,
    ProjectProfile,
    ScopeCitation,
    ScopeExtractionRun,
    ScopeItem,
    TradeDivisionRelevance,
)
from .bilateral_evidence import classify_run as classify_bilateral
from .conflict_resolution import run as resolve_conflicts
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
from .section_extractor import extract_sections_for_project
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
    CSISection,
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


# ============================================================================
# Orchestrator pattern — pluggable scope-extraction modes
# ============================================================================
#
# Replaces the previous 50-line `if mode == "per-discipline" / elif "per-section"
# / elif "hybrid" / else` dispatch chain (and the parallel 17-line chain for
# `sections_total`) with a single Orchestrator class per mode. Adding a new
# mode is one new class, not 30+ LoC of dispatch + denominator branches.
#
# Each Orchestrator owns:
#   - The initial `sections_total` denominator for the progress UI
#   - The execution path: which extractor(s) to run, in what sequence
#   - Its post-processing (e.g. schedule-miner merge, orphan-division handling)
#
# The dispatcher just looks up the class in `_ORCHESTRATORS` and calls
# `run()`. Future modes (e.g. `per-section + drawing_miner` when the miner
# module exists) become a single new class registered in the dict.


from abc import ABC as _ABC, abstractmethod as _abstractmethod
from dataclasses import field as _field


@dataclass
class OrchestratorContext:
    """Everything an Orchestrator needs to execute a run.

    Built once after the run row is created (so run_id is final) and the
    schedule_miner pre-pass has populated `schedule_candidates_by_division`.
    Pass-through container; no behaviour.
    """
    project_id: str
    profile: "ProjectProfile"
    taxonomy: CSITaxonomy
    run_id: str
    active_division_codes: set[str]
    divisions_to_process: list[CSIDivision]
    schedule_candidates_by_division: dict[str, list[CandidateItem]] = _field(default_factory=dict)


@dataclass
class OrchestratorResult:
    """What every Orchestrator returns from `run()`."""
    division_results: list[_DivisionResult]
    discipline_results: list["DisciplineAgentResult"] = _field(default_factory=list)
    cost_usd: float = 0.0


class Orchestrator(_ABC):
    """Mode-pluggable scope-extraction orchestrator.

    Subclass to add a new mode. The dispatcher (run_scope_extraction)
    selects an Orchestrator from `_ORCHESTRATORS[mode]` and calls
    `run(ctx)`. Tests inject a fake Orchestrator directly — the
    interface IS the test surface.
    """

    name: str = ""

    @_abstractmethod
    def initial_sections_total(
        self,
        divisions_to_process: list[CSIDivision],
        active_division_codes: set[str],
    ) -> int:
        """Initial denominator for the progress UI. Computed before the
        run row is created. May be refined inside run() (per-section
        recalculates after filtering empty sections)."""

    @_abstractmethod
    async def run(self, ctx: OrchestratorContext) -> OrchestratorResult: ...


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
                if not isinstance(entry, dict):
                    log.warning(
                        "scope_runner: skipping non-dict spec_without_drawing "
                        "entry in %s (type=%s)", res.discipline, type(entry).__name__,
                    )
                    continue
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
                if not isinstance(entry, dict):
                    continue
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
                if not isinstance(entry, dict):
                    continue
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


async def _merge_schedule_miner_results(
    division_results: list[_DivisionResult],
    schedule_candidates_by_division: dict[str, list[CandidateItem]],
    taxonomy: CSITaxonomy,
) -> None:
    """Bolt schedule_miner outputs onto the LLM-extracted division buckets.

    Two cases:
      - Division already has LLM-emitted items: append miner candidates to
        the same bucket (post-process dedupe handles overlap with the
        deterministic miner rows).
      - Division is fresh (LLM emitted nothing): create a new bucket from
        deduped miner candidates so the schedule's enumerated rows aren't
        silently dropped.

    Mutates `division_results` in place. Used by per-discipline and
    per-section modes; per-division mode does its own merge inline at
    `_process_division`.
    """
    emitted_divisions = {dr.division_code for dr in division_results}
    for div_code, miner_cands in schedule_candidates_by_division.items():
        if not miner_cands:
            continue
        if div_code in emitted_divisions:
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
        # Fresh bucket: LLM emitted nothing for this division
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

    # Hydrate every cited chunk_id in one go so we don't N+1 the DB.
    # Sonnet has been observed under load returning string elements in
    # arrays that are typed as list[dict] (same failure shape link_judge
    # and schedule_miner have defensive checks for). Filter non-dicts
    # rather than crashing the whole run for one malformed item.
    all_chunk_ids: set[str] = set()
    for r in discipline_results:
        for item in r.scope_items:
            if not isinstance(item, dict):
                log.warning(
                    "scope_runner: skipping non-dict scope_item in %s "
                    "result (type=%s, repr=%r)",
                    r.discipline, type(item).__name__, str(item)[:120],
                )
                continue
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
            if not isinstance(item, dict):
                continue  # already warned above
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


async def _run_per_section(
    project_id: str,
    taxonomy: CSITaxonomy,
    run_id: str,
    active_division_codes: set[str],
) -> tuple[list[_DivisionResult], float]:
    """Per-section orchestration: one Sonnet call per CSI section that has
    spec content, bucketed by csi_code[:2] for the existing persistence path.

    Items emerge spec-only (no drawing citations). Drawing evidence is
    layered on later by drawing_grounder when wired (Phase B). The
    bilateral_evidence/evidence_pattern stages downstream correctly handle
    spec-only items per the new evidence-pattern semantics — admin items
    expect spec-only, materials get flagged "missing-drawing" until ground.

    Returns (division_results, total_cost_usd). The persistence path
    (_persist_results) only consumes division_results; we don't generate
    DisciplineAgentResult here so the caller passes [] for that slot.
    """
    # 1. Resolve target sections from the OBSERVED chunk index (the
    #    csi_section column populated by the indexer when a section header
    #    is detected in OCR text), filtered to active divisions.
    #
    #    We can't drive this from the taxonomy alone because Trade_List.xlsx
    #    typically lists sections at the `NN NN 00` parent-code granularity
    #    (e.g. `04 26 00 Concrete Unit Masonry`) while spec authors write
    #    the full 6-digit child code (`04 26 13`). An exact-match against
    #    the taxonomy silently skips ~25 of those orphan-coded sections —
    #    real bid scope like `09 91 13 Exterior Painting`, `07 84 13
    #    Penetration Firestopping`, `12 36 61 Solid Surface Countertops`.
    #    Taxonomy is still used for human-readable titles when available.
    from sqlalchemy import distinct as sql_distinct

    async with SessionLocal() as db:
        observed_codes = set(
            (
                await db.execute(
                    select(sql_distinct(Chunk.csi_section))
                    .join(Document, Chunk.document_id == Document.id)
                    .where(Chunk.project_id == project_id)
                    .where(Document.doc_type == "written-spec")
                    .where(Chunk.csi_section.isnot(None))
                )
            ).scalars().all()
        )

    tax_section_by_code: dict[str, CSISection] = {
        s.code: s for d in taxonomy.divisions for s in d.sections
    }

    target_sections: list[CSISection] = []
    synthetic_count = 0
    for code in sorted(observed_codes):
        if not code or len(code) < 2:
            continue
        div_code = code[:2]
        if div_code not in active_division_codes:
            continue
        existing = tax_section_by_code.get(code)
        if existing is not None:
            target_sections.append(existing)
        else:
            target_sections.append(
                CSISection(
                    code=code,
                    title=f"Section {code}",
                    division_code=div_code,
                )
            )
            synthetic_count += 1

    if not target_sections:
        log.warning(
            "scope_runner[per-section]: no spec-tagged sections in active "
            "divisions %s (observed codes: %d)",
            sorted(active_division_codes), len(observed_codes),
        )
        return [], 0.0

    log.info(
        "scope_runner[per-section]: extracting %d sections "
        "(%d from taxonomy + %d synthetic from observed chunk codes) "
        "— concurrency 8",
        len(target_sections),
        len(target_sections) - synthetic_count,
        synthetic_count,
    )

    # Update the run's sections_total to the *actual* extraction count so
    # the progress UI reaches 100% on completion. The initial value set in
    # run_scope_extraction (sum of all section codes in active divisions)
    # over-counts because most sections never have spec chunks.
    async with SessionLocal() as db:
        run_row = await db.get(ScopeExtractionRun, run_id)
        if run_row is not None:
            run_row.sections_total = len(target_sections)
            await db.commit()

    # 2. Fan out: section_extractor handles concurrency=8 internally.
    all_items, total_cost, by_division_count = await extract_sections_for_project(
        project_id, target_sections
    )
    # Progress UI tracks sections_total; advance once extraction completes.
    # Finer-grained per-section progress would need a callback into
    # section_extractor.
    await _bump_progress(run_id, completed_inc=len(target_sections))

    # Phase B — drawing_grounder. Section_extractor produces spec-only
    # candidates (no drawing citations). The grounder runs Sonnet vision
    # against tiled drawing-sheet renders to find each non-admin item's
    # location/count and appends a synthetic drawing chunk to the item's
    # supporting_chunks. Items that vision can't find stay spec-only and
    # the evidence_pattern rollup correctly marks them missing-drawing.
    # Mutates `all_items` in place.
    if app_settings.drawing_grounder_enabled and all_items:
        from .drawing_grounder import ground_items as _ground_items

        try:
            t_ground = time.perf_counter()
            _, ground_result = await _ground_items(project_id, all_items)
            ground_latency = time.perf_counter() - t_ground
            log.info(
                "scope_runner[per-section]: drawing_grounder — %d/%d items "
                "grounded (%d skipped admin/qc, %d not found on drawings, "
                "%d sheets skipped by cost guard) — spent $%.2f, %.1fs",
                ground_result.items_grounded,
                ground_result.items_total,
                ground_result.items_skipped_admin,
                ground_result.items_unfound,
                ground_result.sheets_skipped_budget,
                ground_result.cost_usd,
                ground_latency,
            )
        except Exception as e:  # noqa: BLE001
            # Grounder failures must not crash the run — items survive as
            # spec-only and surface as missing-drawing in the gap report,
            # which is exactly what we want when the grounder isn't ready.
            log.exception(
                "scope_runner[per-section]: drawing_grounder failed: %s — "
                "items proceed spec-only", e,
            )

    # 3. Bucket by csi_code[:2] into _DivisionResult shape. Ground each
    #    item's csi_code against the project's taxonomy first so persisted
    #    rows are canonicalised.
    by_division: dict[str, list[CandidateItem]] = {}
    for cand in all_items:
        ground = ground_code(cand.csi_code, taxonomy)
        cand.csi_code = ground.code
        setattr(cand, "_ground_method", ground.method)
        setattr(cand, "_ground_note", ground.note)
        # _confidence already set by section_extractor (line 306-307);
        # only fall back to the per-discipline default if missing.
        if not hasattr(cand, "_confidence"):
            setattr(cand, "_confidence", 0.85)
        if not hasattr(cand, "_votes"):
            setattr(cand, "_votes", [])
        div = cand.csi_code[:2]
        by_division.setdefault(div, []).append(cand)

    # 4. Cross-section dedupe inside each division. The kernel groups by
    #    csi_code first (threshold 0.92) so adjacent-code items don't
    #    over-merge.
    division_results: list[_DivisionResult] = []
    for div_code, cands in by_division.items():
        deduped = await dedupe(cands)
        division_results.append(
            _DivisionResult(
                division_code=div_code,
                accepted=deduped,
                candidates_total=len(cands),
                cost_usd=0.0,  # cost rolled up at the section level (total_cost)
            )
        )

    log.info(
        "scope_runner[per-section]: %d sections → %d items across "
        "%d divisions ($%.4f) — by-division: %s",
        len(target_sections), len(all_items), len(by_division),
        total_cost, by_division_count,
    )
    return division_results, total_cost


async def _run_hybrid(
    project_id: str,
    profile: ProjectProfile,
    taxonomy: CSITaxonomy,
    run_id: str,
    active_division_codes: set[str],
) -> tuple[list[_DivisionResult], list[DisciplineAgentResult], float]:
    """Hybrid: per-section + per-discipline, union results, single dedupe pass.

    Solves the architectural gap exposed by Phase A. section_extractor reads
    spec content only — accurate on the architectural side (drywall, painting,
    sealants, firestopping, casework, mirrors) but blind to MEP scope which
    lives entirely on drawing sheets in this project's spec convention.
    discipline_agent reads spec + drawing chunks per discipline — captures
    Div 21/22/26/27/28/31/33 from M0.1, P0.1, E0.1, FP0.1 OCR text.

    Running both and merging via the existing scope_deduper kernel means:
      - section_extractor's wins on the spec side carry through (824 items
        in the latest run vs 317 baseline)
      - discipline_agent's drawing-OCR coverage carries through (~115 MEP
        items the section-only run dropped)
      - dedupe at csi_code group + 0.92 cosine similarity merges items
        the two extractors both find (e.g. drywall in 09 29 00) — citations
        are unioned during cluster merge so the merged item carries BOTH
        spec and drawing evidence

    Cost: ~$3 (per-discipline) + ~$9 (per-section) ≈ $12-13/run. Wall time
    ~12 min sequential (we don't parallelize to avoid burst-rate spikes).
    """
    log.info("scope_runner[hybrid]: starting per-section pass")
    sec_div_results, sec_cost = await _run_per_section(
        project_id, taxonomy, run_id, active_division_codes,
    )

    # Expand sections_total for the progress UI: per-discipline will tick
    # _bump_progress once per discipline. Without this, completed > total.
    disciplines = disciplines_for_project(active_division_codes)
    async with SessionLocal() as db:
        run_row = await db.get(ScopeExtractionRun, run_id)
        if run_row is not None:
            run_row.sections_total = (run_row.sections_total or 0) + len(disciplines)
            await db.commit()

    log.info(
        "scope_runner[hybrid]: starting per-discipline pass (%d disciplines)",
        len(disciplines),
    )
    disc_div_results, disc_results, disc_cost = await _run_per_discipline(
        project_id, profile, taxonomy, run_id, active_division_codes,
    )

    # Union by division_code; concatenate accepted candidates from both
    # extractors, then re-dedupe within each merged bucket.
    by_division: dict[str, list[CandidateItem]] = {}
    candidate_totals: dict[str, int] = {}
    for r in (*sec_div_results, *disc_div_results):
        by_division.setdefault(r.division_code, []).extend(r.accepted)
        candidate_totals[r.division_code] = (
            candidate_totals.get(r.division_code, 0) + r.candidates_total
        )

    merged_results: list[_DivisionResult] = []
    for div_code, cands in by_division.items():
        deduped = await dedupe(cands)
        merged_results.append(
            _DivisionResult(
                division_code=div_code,
                accepted=deduped,
                candidates_total=candidate_totals.get(div_code, 0),
                cost_usd=0.0,
            )
        )

    n_section_items = sum(len(r.accepted) for r in sec_div_results)
    n_discipline_items = sum(len(r.accepted) for r in disc_div_results)
    n_merged = sum(len(r.accepted) for r in merged_results)
    log.info(
        "scope_runner[hybrid]: section=%d items, discipline=%d items, "
        "merged=%d unique items (%d duplicates collapsed) across %d divisions, "
        "$%.4f total ($%.4f section + $%.4f discipline)",
        n_section_items, n_discipline_items, n_merged,
        n_section_items + n_discipline_items - n_merged,
        len(merged_results),
        sec_cost + disc_cost, sec_cost, disc_cost,
    )
    return merged_results, disc_results, sec_cost + disc_cost


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


# ============================================================================
# Admission — pure, no DB.
# ============================================================================


@dataclass
class Admission:
    """Pre-persistence decision for a single CandidateItem.

    `rejection_reason is None` → the candidate is admitted and
    `accepted_chunks` is the (non-empty) list of citations to persist.

    Otherwise the candidate is rejected for one of the named reasons and
    `accepted_chunks` is empty. Persistence MUST skip rejected candidates;
    persisting one would re-introduce the zero-citation orphan bug class.

    `dropped_verdicts` is informational — the (chunk, verdict) pairs L2
    rejected, used by the persistence layer to roll up rejection-reason
    counts for logging.
    """
    candidate: "CandidateItem"
    accepted_chunks: list  # list[RetrievedChunk]
    rejection_reason: str | None
    dropped_verdicts: list  # list[tuple[RetrievedChunk, CitationVerdict]]


def _admit_candidate(cand: "CandidateItem") -> Admission:
    """Pure admission decision — no DB, no I/O.

    Schedule-miner candidates skip L2 entirely: their synthetic per-row
    chunks are deterministic and already passed the miner's hallucination
    guard. All other candidates must produce at least one citation that
    survives `filter_valid_citations` (description-token-overlap check),
    or the candidate is rejected.

    The interface is the test surface: pass synthetic CandidateItems with
    in-memory RetrievedChunks → assert on the returned Admission. No DB
    session, no API key, no fixtures.
    """
    if cand.extraction_method == "schedule_miner":
        return Admission(
            candidate=cand,
            accepted_chunks=list(cand.supporting_chunks),
            rejection_reason=None,
            dropped_verdicts=[],
        )

    if not cand.supporting_chunks:
        return Admission(
            candidate=cand,
            accepted_chunks=[],
            rejection_reason="no_supporting_chunks",
            dropped_verdicts=[],
        )

    kept, dropped = filter_valid_citations(
        cand.description, cand.supporting_chunks
    )
    if not kept:
        return Admission(
            candidate=cand,
            accepted_chunks=[],
            rejection_reason="all_l2_rejected",
            dropped_verdicts=dropped,
        )

    return Admission(
        candidate=cand,
        accepted_chunks=kept,
        rejection_reason=None,
        dropped_verdicts=dropped,
    )


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
    items_zeroed = 0  # items that lost ALL citations to validation — NOT persisted
    items_rejected_no_citations = 0  # items the agent emitted with empty supporting_chunks

    # ====================================================================
    # Phase 1 — Admission (pure, no DB).
    # Decide per candidate: admit + which citations to persist, or reject.
    # The bug fix that previously lived inline (move validation BEFORE
    # db.add) is now expressed as a structural property: persistence only
    # ever sees `Admission(rejection_reason=None)` rows. The orphan-item
    # bug class is impossible by construction.
    # ====================================================================
    admissions_by_division: dict[str, list[Admission]] = {}
    for d in division_results:
        admissions_by_division[d.division_code] = [
            _admit_candidate(c) for c in d.accepted
        ]

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
            for adm in admissions_by_division.get(d.division_code, []):
                # Roll up dropped-citation reasons regardless of admission
                # outcome — useful for both kept and rejected items so the
                # log line reflects total L2 work done.
                for _, verdict in adm.dropped_verdicts:
                    citations_dropped[verdict.reason] = (
                        citations_dropped.get(verdict.reason, 0) + 1
                    )

                # Phase 1 already decided. Just act on it.
                if adm.rejection_reason is not None:
                    if adm.rejection_reason == "all_l2_rejected":
                        items_zeroed += 1
                        log.info(
                            "citation_validator: rejecting candidate (lost "
                            "ALL %d citations to L2) — desc=%r reasons=%s",
                            len(adm.candidate.supporting_chunks),
                            (adm.candidate.description or "")[:80],
                            [v.reason for _, v in adm.dropped_verdicts],
                        )
                    else:  # no_supporting_chunks
                        items_rejected_no_citations += 1
                        log.info(
                            "scope_runner: rejecting candidate emitted with "
                            "zero supporting_chunks — desc=%r",
                            (adm.candidate.description or "")[:80],
                        )
                    continue

                cand = adm.candidate
                chunks_to_persist = adm.accepted_chunks

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
    if total_dropped or items_zeroed or items_rejected_no_citations:
        log.info(
            "citation_validator: kept=%d dropped=%d (empty=%d header=%d "
            "low_overlap=%d) items_zeroed=%d items_no_citations=%d",
            citations_kept,
            total_dropped,
            citations_dropped.get("EMPTY", 0),
            citations_dropped.get("HEADER_ONLY", 0),
            citations_dropped.get("LOW_OVERLAP", 0),
            items_zeroed,
            items_rejected_no_citations,
        )
    # Adjust the deduped_total return so it reflects what actually got
    # persisted (was over-counting by items_zeroed + items_rejected_no_citations).
    deduped_total -= items_zeroed + items_rejected_no_citations
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
        "conflict_resolution": "claude-opus-4-7",
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


# ============================================================================
# Concrete Orchestrators — one class per mode.
# ============================================================================
#
# Each wraps the existing `_run_per_*` private helpers + their post-processing
# (schedule-miner merge, orphan-division handling). The helper functions stay
# untouched; the Orchestrator class is the test surface.


class PerDisciplineOrchestrator(Orchestrator):
    name = "per-discipline"

    def initial_sections_total(self, divisions_to_process, active_division_codes):
        # ~10 disciplines mapped from the active division codes.
        return len(disciplines_for_project(active_division_codes)) or 1

    async def run(self, ctx: OrchestratorContext) -> OrchestratorResult:
        div_results, disc_results, cost = await _run_per_discipline(
            ctx.project_id, ctx.profile, ctx.taxonomy,
            ctx.run_id, ctx.active_division_codes,
        )
        await _merge_schedule_miner_results(
            div_results, ctx.schedule_candidates_by_division, ctx.taxonomy,
        )
        return OrchestratorResult(div_results, disc_results, cost)


class PerSectionOrchestrator(Orchestrator):
    name = "per-section"

    def initial_sections_total(self, divisions_to_process, active_division_codes):
        # Sum of sections in active divisions; per-section's run() refines
        # this to the count of sections that actually have spec content.
        return sum(len(d.sections) for d in divisions_to_process) or 1

    async def run(self, ctx: OrchestratorContext) -> OrchestratorResult:
        div_results, cost = await _run_per_section(
            ctx.project_id, ctx.taxonomy, ctx.run_id, ctx.active_division_codes,
        )
        await _merge_schedule_miner_results(
            div_results, ctx.schedule_candidates_by_division, ctx.taxonomy,
        )
        return OrchestratorResult(div_results, [], cost)


class HybridOrchestrator(Orchestrator):
    name = "hybrid"

    def initial_sections_total(self, divisions_to_process, active_division_codes):
        # Both phases tick: per-section sections + per-discipline disciplines.
        return (
            sum(len(d.sections) for d in divisions_to_process)
            + len(disciplines_for_project(active_division_codes))
        ) or 1

    async def run(self, ctx: OrchestratorContext) -> OrchestratorResult:
        div_results, disc_results, cost = await _run_hybrid(
            ctx.project_id, ctx.profile, ctx.taxonomy,
            ctx.run_id, ctx.active_division_codes,
        )
        await _merge_schedule_miner_results(
            div_results, ctx.schedule_candidates_by_division, ctx.taxonomy,
        )
        return OrchestratorResult(div_results, disc_results, cost)


class PerDivisionOrchestrator(Orchestrator):
    """Legacy mode — process each division in parallel through the
    Stage A/B/C/D pipeline (`_process_division`). schedule_miner items
    are incorporated INSIDE _process_division, so this orchestrator does
    NOT call the shared `_merge_schedule_miner_results` helper — that
    would double-count them. Instead it surfaces orphan-division miner
    items at the end.
    """
    name = "per-division"

    def initial_sections_total(self, divisions_to_process, active_division_codes):
        return len(divisions_to_process)

    async def run(self, ctx: OrchestratorContext) -> OrchestratorResult:
        sem_div = asyncio.Semaphore(_DIVISION_CONCURRENCY)
        sem_validate = asyncio.Semaphore(_VALIDATE_CONCURRENCY)

        async def _process_with_sem(division: CSIDivision) -> _DivisionResult:
            async with sem_div:
                try:
                    return await _process_division(
                        ctx.project_id, division, ctx.profile, ctx.taxonomy,
                        ctx.run_id, sem_validate,
                        ctx.schedule_candidates_by_division.get(division.code, []),
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "scope_runner: division %s failed", division.code,
                    )
                    await _bump_progress(ctx.run_id, failed_inc=1)
                    return _DivisionResult(division.code, [], 0, 0.0)

        division_results = list(await asyncio.gather(
            *(_process_with_sem(d) for d in ctx.divisions_to_process)
        ))

        # Surface orphan-division schedule-miner items (their division
        # wasn't in the relevance set, but the miner found a schedule
        # whose rows fell into it; valuable line items, don't drop).
        relevant_codes = {d.code for d in ctx.divisions_to_process}
        orphan_divisions = (
            set(ctx.schedule_candidates_by_division) - relevant_codes
        )
        for div_code in orphan_divisions:
            orphan_cands = ctx.schedule_candidates_by_division[div_code]
            if not orphan_cands:
                continue
            log.info(
                "scope_runner: surfacing %d schedule-miner items in "
                "non-relevant div %s",
                len(orphan_cands), div_code,
            )
            for c in orphan_cands:
                ground = ground_code(c.csi_code, ctx.taxonomy)
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

        return OrchestratorResult(division_results, [], 0.0)


_ORCHESTRATORS: dict[str, type[Orchestrator]] = {
    "per-discipline": PerDisciplineOrchestrator,
    "per-section": PerSectionOrchestrator,
    "hybrid": HybridOrchestrator,
    "per-division": PerDivisionOrchestrator,
}


def _get_orchestrator(mode: str) -> Orchestrator:
    """Return an Orchestrator instance for the requested mode, or fall
    back to PerDisciplineOrchestrator for unknown values (with a warning)."""
    cls = _ORCHESTRATORS.get(mode)
    if cls is None:
        log.warning(
            "scope_runner: unknown scope_orchestration_mode=%r, falling "
            "back to per-discipline (valid modes: %s)",
            mode, sorted(_ORCHESTRATORS.keys()),
        )
        cls = PerDisciplineOrchestrator
    return cls()


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

    # Hygiene pass: clear stale True-overrides on divisions where every
    # signal disagrees (LLM=False AND no corpus evidence AND override
    # forced True). This prevents the missing_division gap report from
    # being polluted by leftover UI override clicks on irrelevant
    # divisions (Div 35 Marine, Div 44 Pollution Control, etc. on a
    # community center). Legitimate operator overrides — where the
    # user is adding scope they know about from external context —
    # typically don't match all three conditions, so this is safe by
    # default. Failures are logged but don't block the run.
    try:
        from .trade_filter import reevaluate_corpus_evidence

        cleanup = await reevaluate_corpus_evidence(
            project_id, clear_stale_true_overrides=True,
        )
        if cleanup.get("cleared_overrides") or cleanup.get("flipped"):
            log.info(
                "scope_runner: relevance hygiene — flipped is_relevant=%s, "
                "cleared stale True-overrides=%s",
                cleanup.get("flipped") or [],
                cleanup.get("cleared_overrides") or [],
            )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "scope_runner: relevance hygiene pass failed (proceeding "
            "with whatever state is in the DB): %s", e,
        )

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

    # Sections_total denominator is mode-dependent so the progress UI
    # ticks at the right rate:
    #   per-discipline → ~10 disciplines
    #   per-division → ~20-30 divisions
    #   per-section → ~80-150 sections (much finer-grained)
    #   hybrid → per-section count + per-discipline count (both phases)
    mode = (app_settings.scope_orchestration_mode or "per-discipline").strip()
    orchestrator = _get_orchestrator(mode)
    # Re-read effective mode from the orchestrator (handles "fell back to
    # per-discipline" case so the run config records the actual mode used).
    mode = orchestrator.name

    active_division_codes = {d.code for d in divisions_to_process}
    sections_total = orchestrator.initial_sections_total(
        divisions_to_process, active_division_codes,
    )

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

    # Single dispatch — Orchestrator class owns its own execution path,
    # post-processing, and (where applicable) schedule-miner merge logic.
    ctx = OrchestratorContext(
        project_id=project_id,
        profile=profile,
        taxonomy=taxonomy,
        run_id=run_id,
        active_division_codes=active_division_codes,
        divisions_to_process=divisions_to_process,
        schedule_candidates_by_division=schedule_candidates_by_division,
    )
    orchestrator_result = await orchestrator.run(ctx)
    division_results = orchestrator_result.division_results
    discipline_results = orchestrator_result.discipline_results
    discipline_cost = orchestrator_result.cost_usd

    total_latency_ms = int((time.perf_counter() - t0) * 1000)

    candidates, validated, deduped = await _persist_results(
        run_id, project_id, division_results, taxonomy
    )

    # Per-discipline mode also persists the agent's spec-without-drawing /
    # drawing-without-spec / unilateral_items as Gap rows so HITL sees them.
    if mode in ("per-discipline", "hybrid") and discipline_results:
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

    # Stage F3 — Conflict resolution. Detects within-CSI qty/unit
    # disagreements + cross-division overlaps via embedding clustering
    # (no LLM), then auto-arbitrates clusters where every member's
    # confidence > 0.8 via one Opus 4.7 call each. Lower-confidence
    # clusters stay status='open' for the HITL queue. Two-phase commit
    # internally — detection persists before arbitration runs, so an
    # Opus failure mid-batch still leaves conflicts visible to humans.
    conflict_result = await resolve_conflicts(run_id)
    arbitration_cost = conflict_result.cost_usd
    log.info(
        "scope_runner: conflicts — detected qty=%d unit=%d cross_div=%d, "
        "arbitrated=%d deferred=%d to HITL, $%.3f",
        conflict_result.qty_mismatch,
        conflict_result.unit_mismatch,
        conflict_result.cross_division,
        conflict_result.arbitrated,
        conflict_result.deferred,
        conflict_result.cost_usd,
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
