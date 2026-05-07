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
from .discipline_config import is_valid_sheet_id
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
        # Only flag sections that:
        #   (a) live in a covered division (has ≥1 item already), AND
        #   (b) are EXPLICITLY MENTIONED in the project's spec book —
        #       i.e. some spec chunk's text contains the canonical
        #       "NN NN NN" code or its compact "NN NN NN" form.
        # Reason: the CSI catalog has ~5,000 sections; only ~50-200 are
        # in scope on any given project. The catalog itself is not a
        # signal — the spec author's TOC is. If the spec doesn't mention
        # a section, it's not in scope, and flagging it as missing is
        # noise.
        # Skip "00 00" division-root codes — they're bookkeeping.
        taxonomy = await get_taxonomy_for_project(project_id)
        if taxonomy is not None:
            sections_with_items = {i.csi_code for i in items}
            covered_divisions = {i.csi_division for i in items}

            # Pre-fetch all spec chunk texts for the project so we can
            # check section mentions without per-section query.
            from sqlalchemy import select as _select

            from ..models import Chunk, Document as _Doc

            spec_text_blob = ""
            spec_chunks_q = (
                _select(Chunk.text)
                .join(_Doc, Chunk.document_id == _Doc.id)
                .where(Chunk.project_id == project_id)
                .where(_Doc.doc_type == "written-spec")
            )
            spec_texts = (await db.execute(spec_chunks_q)).scalars().all()
            if spec_texts:
                # Concatenated (deduped on first chunk only would miss
                # late-spec sections — keep all).
                spec_text_blob = "\n".join(t for t in spec_texts if t)

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
                    # Spec-mention filter: only flag when the spec
                    # explicitly mentions this section.
                    if not spec_text_blob:
                        # No spec uploaded → skip this signal entirely.
                        # Otherwise a drawings-only project would generate
                        # noise on every catalog section.
                        continue
                    if section.code not in spec_text_blob:
                        continue
                    db.add(
                        Gap(
                            project_id=project_id,
                            run_id=run_id,
                            gap_type="missing_section",
                            csi_division=div_code,
                            csi_section=section.code,
                            description=(
                                f"Section {section.code} ({section.title}) is "
                                f"referenced in the spec book but no scope "
                                f"items were extracted for it."
                            ),
                            severity="warn",
                            suggested_remediation=(
                                "Open the spec section in the document viewer "
                                "and confirm whether items should be added; "
                                "this is a likely coverage miss since the spec "
                                "explicitly calls out the section."
                            ),
                        )
                    )
                    stats.missing_section += 1

        # ---- 3. evidence-pattern shortfall — DEPRECATED ----
        # Previously emitted one Gap row per (division, section) bucket of
        # INFERRED_LOW_CONFIDENCE items. Removed because:
        #   1. The evidence_tier=INFERRED_LOW_CONFIDENCE badge already
        #      surfaces these items directly on the scope page, with full
        #      drill-down on WHY each item didn't meet its expected pattern
        #      (bilateral_evidence + evidence_pattern columns).
        #   2. Many flagged items are CORRECTLY one-sided per their
        #      expected_pattern — admin items expect spec_only, demo items
        #      expect drawing_only, and MEP items in a project where the
        #      spec book has no MEP sections legitimately expect
        #      drawing_only. Re-surfacing them as gaps creates noise that
        #      conflates "system uncertainty" with "real coverage problem."
        # Reviewers who want a low-confidence review queue can filter the
        # scope view by evidence_tier directly — no duplicate gap row needed.
        # stats.unilateral_evidence stays at 0; downstream consumers
        # treating its absence as "no shortfall" still work correctly.

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
        # De-dupe by (target_sheet, detail_id) so each broken link is one gap.
        # Also drop cross-refs whose target isn't a real sheet ID — the vision
        # extractor sometimes captures discipline names verbatim (e.g.
        # "STRUCTURAL", "Architectural drawings") when a note reads "see
        # structural" without naming a sheet. These aren't broken links;
        # they're just narrative pointers.
        seen: set[tuple[str, str | None]] = set()
        for xref in xref_rows:
            if not is_valid_sheet_id(xref.target_sheet):
                continue
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
