"""Stage 6 — project trust score (7-component, design-doc spec).

The reference design (`Elks_AI_Pipeline_Plan.pdf`) defines a 7-component
trust score:

    OCR confidence (20%)        — requires triple-OCR ensemble (W1)
    Spec coverage (20%)         — kept here as spec_section_coverage
    Cross-reference rate (20%)  — kept here as bilateral_coverage
    Schedule extraction (15%)   — requires per-schedule-type schemas
    Symbol detection (10%)      — requires YOLO11 (W3)
    Citation entailment (10%)   — kept here as link_judge_pass_rate
    Version consistency (5%)    — requires multi-revision parser (W18)

This implementation drops the four components that depend on infrastructure
explicitly out of scope (W1 / W3 / W6 / W18) and rebalances the remaining
three around a fourth: the validator's average extraction confidence. The
substitution is structurally equivalent — dual evidence (40%), extraction
quality (20%), post-hoc validation (20%), spec coverage (20%) — and the
weights are recorded in ``ScopeExtractionRun.trust_score_components`` so
future re-tunes are config-only, not migrations.

Tiers:
    GREEN  >= 0.80  — bid-ready first draft
    YELLOW 0.60-0.79 — useful but needs active review
    RED    <  0.60  — not bid-ready
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from ..database import SessionLocal
from ..models import (
    Conflict,
    Document,
    DocumentPage,
    ExtractedSchedule,
    Gap,
    PageExtraction,
    Project,
    ScopeCitation,
    ScopeExtractionRun,
    ScopeItem,
    SheetRevision,
    TradeDivisionRelevance,
    TradePackage,
)
from .trade_list_parser import get_taxonomy_for_project

log = logging.getLogger(__name__)


# Weights — sum to 1.0. Persisted into trust_score_components for audit.
# Six wired components per the design doc; symbol_detection_confidence
# is the seventh and stays dropped until a fine-tuned YOLO11 model is
# installed (the stub returns empty so its score would be uninformative).
_WEIGHTS = {
    "bilateral_coverage": 0.30,
    "extraction_confidence_avg": 0.15,
    "link_judge_pass_rate": 0.15,
    "spec_section_coverage": 0.15,
    "ocr_text_coverage": 0.10,
    "schedule_extraction_validity": 0.05,
    "document_version_consistency": 0.10,
}

# Tier thresholds
GREEN_MIN = 0.80
YELLOW_MIN = 0.60


@dataclass
class TrustScoreResult:
    score: float
    tier: str
    components: dict


def _classify_tier(score: float) -> str:
    if score >= GREEN_MIN:
        return "GREEN"
    if score >= YELLOW_MIN:
        return "YELLOW"
    return "RED"


async def compute_run(run_id: str) -> TrustScoreResult:
    """Compute the 4-component trust score for one ScopeExtractionRun.

    Persists score + per-component values onto the ScopeExtractionRun row
    and mirrors the score onto Project.trust_score_latest. Returns the
    result for callers that want to surface it immediately.
    """
    async with SessionLocal() as db:
        run = await db.get(ScopeExtractionRun, run_id)
        if run is None:
            raise ValueError(f"trust_score: run {run_id} not found")

        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        if not items:
            log.info("trust_score: no items in run %s", run_id)
            return TrustScoreResult(0.0, "RED", {"reason": "no_items"})

        project_id = items[0].project_id

        # ---- Component 1: evidence-pattern coverage rate ----
        # Replaces the raw bilateral count. An item counts toward coverage
        # when it meets its EXPECTED pattern (spec_only / drawing_only /
        # bilateral) — derived from CSI section + admin-keyword override.
        # See bilateral_evidence._classify and evidence_pattern module.
        # We read the per-item rationale that bilateral_evidence wrote
        # into trust_components.pattern_match. Falls back to the legacy
        # bilateral flag when an item hasn't been classified yet.
        total_items = len(items)
        def _matched(item) -> bool:
            tc = item.trust_components or {}
            if "pattern_match" in tc:
                return bool(tc["pattern_match"])
            return bool(item.bilateral_evidence)
        bilateral_items = sum(1 for i in items if _matched(i))
        bilateral_rate = bilateral_items / total_items if total_items else 0.0

        # ---- Component 2: extraction confidence average ----
        confidences = [i.confidence for i in items if i.confidence is not None]
        confidence_avg = sum(confidences) / len(confidences) if confidences else 0.0

        # ---- Component 3: link-judge pass rate ----
        cit_rows = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_([i.id for i in items])
                )
            )
        ).scalars().all()
        judged = [c for c in cit_rows if c.is_link_judge_pass is not None]
        if judged:
            link_judge_pass_rate = (
                sum(1 for c in judged if c.is_link_judge_pass) / len(judged)
            )
        else:
            # Link judge hasn't run yet — give the rate a neutral 0.5 so it
            # doesn't tank the overall score before the data exists.
            link_judge_pass_rate = 0.5

        # ---- Component 4: spec section coverage ----
        # Numerator: distinct csi_codes present in scope items
        # Denominator: distinct csi_section codes in the project's relevant
        # divisions (excluding "00 00" division roots).
        relevance_rows = (
            await db.execute(
                select(TradeDivisionRelevance).where(
                    TradeDivisionRelevance.project_id == project_id
                )
            )
        ).scalars().all()
        relevant_divisions = {
            r.csi_division for r in relevance_rows if r.effective_relevance
        }

        taxonomy = await get_taxonomy_for_project(project_id)
        if taxonomy is not None and relevant_divisions:
            expected_sections: set[str] = set()
            for div_code in relevant_divisions:
                div = taxonomy.get_division(div_code)
                if div is None:
                    continue
                for section in div.sections:
                    if section.code.endswith(" 00 00"):
                        continue
                    expected_sections.add(section.code)
            present_sections = {
                i.csi_code for i in items if not i.csi_code.endswith(" 00 00")
            }
            spec_section_coverage = (
                len(present_sections & expected_sections) / len(expected_sections)
                if expected_sections
                else 1.0
            )
        else:
            # No taxonomy — treat coverage as 1.0 (don't penalize missing
            # data we can't measure).
            spec_section_coverage = 1.0

        # ---- Component 5: OCR text coverage ----
        # % of pages on text-bearing docs (written-spec / bid-quote /
        # scope-letter) that successfully landed text via OCR or native
        # extraction. Empty => neutral 1.0 (don't penalize a project
        # that uploaded only drawings — those go through vision, not OCR).
        from sqlalchemy import func as sql_func

        ocr_pages_total = (
            await db.execute(
                select(sql_func.count(DocumentPage.id))
                .join(Document, DocumentPage.document_id == Document.id)
                .where(Document.project_id == project_id)
                .where(Document.doc_type.in_(("written-spec", "bid-quote", "scope-letter")))
            )
        ).scalar() or 0
        ocr_pages_with_text = (
            await db.execute(
                select(sql_func.count(DocumentPage.id))
                .join(Document, DocumentPage.document_id == Document.id)
                .where(Document.project_id == project_id)
                .where(Document.doc_type.in_(("written-spec", "bid-quote", "scope-letter")))
                .where(DocumentPage.text_content.isnot(None))
            )
        ).scalar() or 0
        ocr_text_coverage = (
            ocr_pages_with_text / ocr_pages_total
            if ocr_pages_total
            else 1.0
        )

        # ---- Component 6: schedule extraction validity ----
        # % of typed schedules (extracted via schedule_extractor_typed) that
        # produced > 0 rows. Empty schedules indicate vision misread or an
        # OCR boundary failure on the schedule grid.
        sched_rows = (
            await db.execute(
                select(ExtractedSchedule)
                .join(PageExtraction, ExtractedSchedule.page_extraction_id == PageExtraction.id)
                .join(Document, PageExtraction.document_id == Document.id)
                .where(Document.project_id == project_id)
                .where(ExtractedSchedule.name.like("[typed:%"))
            )
        ).scalars().all()
        if sched_rows:
            valid = sum(1 for s in sched_rows if (s.rows or []))
            schedule_extraction_validity = valid / len(sched_rows)
        else:
            schedule_extraction_validity = 1.0  # No typed schedules → neutral

        # ---- Component 7: document version consistency ----
        # When multiple drawing-set docs share a sheet_id, do they agree
        # on the latest revision? A divergence (e.g. doc A says Rev 2 of
        # A1.1, doc B says Rev 1) is a strong signal that the GC has
        # mismatched bid-set documents — should be reconciled before bid.
        rev_rows = (
            await db.execute(
                select(SheetRevision)
                .where(SheetRevision.project_id == project_id)
            )
        ).scalars().all()
        if rev_rows:
            # Group max revision per (sheet_id, document_id), then check
            # if all documents agree on the same max for each sheet_id.
            from collections import defaultdict

            by_sheet_doc: dict[tuple[str, str], str] = {}
            for r in rev_rows:
                key = (r.sheet_id, r.document_id)
                # rev_number is a string; max-by-string sort is good enough
                # for the typical 0/1/2/3/A/B sequence
                prev = by_sheet_doc.get(key)
                if prev is None or (r.rev_number or "") > prev:
                    by_sheet_doc[key] = r.rev_number or ""
            # Group by sheet_id
            by_sheet: dict[str, set[str]] = defaultdict(set)
            for (sheet_id, _doc_id), rev in by_sheet_doc.items():
                by_sheet[sheet_id].add(rev)
            multi_doc_sheets = [
                revs for revs in by_sheet.values() if len(revs) > 0
            ]
            if multi_doc_sheets:
                consistent = sum(1 for revs in multi_doc_sheets if len(revs) == 1)
                document_version_consistency = consistent / len(multi_doc_sheets)
            else:
                document_version_consistency = 1.0
        else:
            # No revision data parsed → neutral (don't penalise; revision
            # parser may have been disabled or no docs uploaded yet).
            document_version_consistency = 1.0

        # ---- Final weighted score ----
        components = {
            "bilateral_coverage": round(bilateral_rate, 4),
            "extraction_confidence_avg": round(confidence_avg, 4),
            "link_judge_pass_rate": round(link_judge_pass_rate, 4),
            "spec_section_coverage": round(spec_section_coverage, 4),
            "ocr_text_coverage": round(ocr_text_coverage, 4),
            "schedule_extraction_validity": round(schedule_extraction_validity, 4),
            "document_version_consistency": round(document_version_consistency, 4),
        }
        score = sum(components[k] * _WEIGHTS[k] for k in _WEIGHTS)
        score = round(score, 4)
        tier = _classify_tier(score)

        # Auxiliary rollups so the run + dashboard can render without joins.
        conflict_count = (
            await db.execute(
                select(Conflict).where(Conflict.run_id == run_id)
            )
        ).scalars().all()
        gap_count = (
            await db.execute(
                select(Gap).where(Gap.run_id == run_id)
            )
        ).scalars().all()
        package_count = (
            await db.execute(
                select(TradePackage).where(TradePackage.run_id == run_id)
            )
        ).scalars().all()

        # Persist on the run
        run.trust_score = score
        run.trust_score_components = {
            "components": components,
            "weights": _WEIGHTS,
            "tier": tier,
            "tier_thresholds": {"GREEN": GREEN_MIN, "YELLOW": YELLOW_MIN},
            "dropped_components": [
                "symbol_detection_confidence",
            ],
            "rationale": (
                "6-of-7 design-doc components wired. "
                "symbol_detection_confidence dropped until a fine-tuned "
                "YOLO11 MEP model is installed (current stub returns no "
                "detections, so its score would be uninformative)."
            ),
        }
        run.bilateral_coverage_rate = components["bilateral_coverage"]
        run.link_judge_pass_rate = components["link_judge_pass_rate"]
        run.spec_section_coverage_rate = components["spec_section_coverage"]
        run.conflict_count = len(conflict_count)
        run.gap_count = len(gap_count)
        run.package_count = len(package_count)

        # Mirror on Project for the project list page
        project = await db.get(Project, project_id)
        if project is not None:
            project.trust_score_latest = score

        await db.commit()

    log.info(
        "trust_score: run %s — score=%.3f (%s) bilateral=%.2f conf=%.2f "
        "link_judge=%.2f spec_cov=%.2f",
        run_id,
        score,
        tier,
        components["bilateral_coverage"],
        components["extraction_confidence_avg"],
        components["link_judge_pass_rate"],
        components["spec_section_coverage"],
    )
    return TrustScoreResult(score=score, tier=tier, components=components)
