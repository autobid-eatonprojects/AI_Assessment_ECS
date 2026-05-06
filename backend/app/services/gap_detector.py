"""Stage 5 — gap detection.

Surfaces "what's missing" as first-class Gap rows for HITL review. Four types:

    missing_division
        TradeDivisionRelevance.effective_relevance == True for a division
        that produced zero ScopeItems in this run. Either the relevance
        filter was wrong or extraction missed the division entirely.
        Severity: blocker.

    missing_section
        A CSI section in a relevant division that has no ScopeItems.
        Skipped for non-leaf "00 00" division-root codes — those are
        bookkeeping rather than biddable scope. Severity: warn.

    unilateral_evidence
        ScopeItem with evidence_tier='INFERRED_LOW_CONFIDENCE' (already
        flagged by Stage 2 bilateral classifier — surfacing them as Gap
        rows lets the HITL Gaps tab show them alongside the structural
        gaps). Severity: info.

    unresolved_cross_reference
        ExtractedCrossReference (drawing says "see S2.1") whose target
        isn't cited by any ScopeItem. Suggests scope was missed for
        that referenced sheet. Severity: warn.

No LLM calls. Rebuilds gap rows for the run on each invocation (idempotent).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import delete, select

from ..database import SessionLocal
from ..models import (
    Document,
    ExtractedCrossReference,
    Gap,
    PageExtraction,
    ScopeCitation,
    ScopeItem,
    TradeDivisionRelevance,
)
from .trade_list_parser import get_taxonomy_for_project

log = logging.getLogger(__name__)


@dataclass
class GapStats:
    missing_division: int
    missing_section: int
    unilateral_evidence: int
    unresolved_cross_reference: int

    @property
    def total(self) -> int:
        return (
            self.missing_division
            + self.missing_section
            + self.unilateral_evidence
            + self.unresolved_cross_reference
        )


async def detect_gaps(run_id: str) -> GapStats:
    """Walk a run and write Gap rows for what's missing."""
    stats = GapStats(0, 0, 0, 0)

    async with SessionLocal() as db:
        items = (
            await db.execute(select(ScopeItem).where(ScopeItem.run_id == run_id))
        ).scalars().all()
        if not items:
            return stats
        project_id = items[0].project_id

        # Wipe prior gap rows for this run (idempotent)
        await db.execute(delete(Gap).where(Gap.run_id == run_id))
        await db.flush()

        # ---- 1. missing_division ----
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
        divisions_with_items = {i.csi_division for i in items}
        missing_divs = sorted(relevant_divisions - divisions_with_items)
        for div_code in missing_divs:
            label = next(
                (r.division_label for r in relevance_rows if r.csi_division == div_code),
                f"Division {div_code}",
            )
            db.add(
                Gap(
                    project_id=project_id,
                    run_id=run_id,
                    gap_type="missing_division",
                    csi_division=div_code,
                    description=(
                        f"{label} was marked relevant but produced zero scope "
                        f"items. Either the relevance filter is wrong or the "
                        f"extractor missed the division entirely."
                    ),
                    severity="blocker",
                    suggested_remediation=(
                        f"Re-run scope extraction targeting {label} only, "
                        f"or override relevance to false if division genuinely "
                        f"isn't in scope."
                    ),
                )
            )
            stats.missing_division += 1

        # ---- 2. missing_section ----
        # Only enumerate sections in divisions that ALREADY have at least
        # one scope item. Divisions with zero items are caught by
        # missing_division (#1 above); enumerating every section in those
        # is noise (1000+ false positives on a typical project, since the
        # CSI catalog has ~5000 sections and most projects only touch a
        # few hundred).
        # Within a covered division, an unmatched section IS a real gap —
        # the division is in-scope, but the spec doesn't reference this
        # particular section and the drawings don't depict it.
        # Skip "00 00" division-root codes — they're bookkeeping.
        taxonomy = await get_taxonomy_for_project(project_id)
        if taxonomy is not None:
            sections_with_items = {i.csi_code for i in items}
            covered_divisions = {i.csi_division for i in items}
            for div_code in relevant_divisions:
                if div_code not in covered_divisions:
                    continue
                div = taxonomy.get_division(div_code)
                if div is None:
                    continue
                for section in div.sections:
                    if section.code.endswith(" 00 00"):
                        continue
                    if section.code in sections_with_items:
                        continue
                    db.add(
                        Gap(
                            project_id=project_id,
                            run_id=run_id,
                            gap_type="missing_section",
                            csi_division=div_code,
                            csi_section=section.code,
                            description=(
                                f"Section {section.code} ({section.title}) is in "
                                f"a covered division ({div_code}) but has no "
                                f"scope items in this run."
                            ),
                            severity="info",  # was 'warn' — most are legit non-coverage
                            suggested_remediation=(
                                "Verify whether this section applies to the "
                                "project; if so, surface manually."
                            ),
                        )
                    )
                    stats.missing_section += 1

        # ---- 3. unilateral_evidence ----
        unilateral_items = [
            i for i in items if i.evidence_tier == "INFERRED_LOW_CONFIDENCE"
        ]
        for item in unilateral_items:
            db.add(
                Gap(
                    project_id=project_id,
                    run_id=run_id,
                    gap_type="unilateral_evidence",
                    csi_division=item.csi_division,
                    csi_section=item.csi_code,
                    description=(
                        f"Item lacks bilateral evidence (drawing AND spec). "
                        f"Description: {item.description[:140]}"
                    ),
                    severity="info",
                    suggested_remediation=(
                        "Operator should verify item in HITL low-confidence "
                        "queue; either accept, reject, or escalate via RFI."
                    ),
                    related_item_id=item.id,
                )
            )
            stats.unilateral_evidence += 1

        # ---- 4. unresolved_cross_reference ----
        # Pull all cross-refs for this project's documents. Compare against
        # the set of (sheet_number) values cited by scope items in this run.
        cited_sheets = {c.sheet_number for c in (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_([i.id for i in items])
                )
            )
        ).scalars().all() if c.sheet_number}

        # Cross-refs hang off PageExtraction → Document → Project. JOIN
        # through Document to filter by project.
        xref_rows = (
            await db.execute(
                select(ExtractedCrossReference)
                .join(
                    PageExtraction,
                    ExtractedCrossReference.page_extraction_id == PageExtraction.id,
                )
                .join(
                    Document,
                    PageExtraction.document_id == Document.id,
                )
                .where(Document.project_id == project_id)
            )
        ).scalars().all()
        # De-dupe by (target_sheet, detail_id) so each broken link is one gap
        seen: set[tuple[str, str | None]] = set()
        for xref in xref_rows:
            key = (xref.target_sheet, xref.detail_id)
            if key in seen:
                continue
            seen.add(key)
            if xref.target_sheet in cited_sheets:
                continue
            detail = f" detail {xref.detail_id}" if xref.detail_id else ""
            db.add(
                Gap(
                    project_id=project_id,
                    run_id=run_id,
                    gap_type="unresolved_cross_reference",
                    description=(
                        f"Drawing references {xref.target_sheet}{detail} but "
                        f"no scope item cites that sheet. "
                        f"Context: {(xref.context or '')[:140]}"
                    ),
                    severity="warn",
                    suggested_remediation=(
                        f"Issue RFI: confirm {xref.target_sheet}{detail} is "
                        "reflected in the scope or is informational only."
                    ),
                )
            )
            stats.unresolved_cross_reference += 1

        await db.commit()

    log.info(
        "gap_detector: run %s — missing_div=%d missing_sec=%d "
        "unilateral=%d cross_ref=%d (total %d)",
        run_id,
        stats.missing_division,
        stats.missing_section,
        stats.unilateral_evidence,
        stats.unresolved_cross_reference,
        stats.total,
    )
    return stats
