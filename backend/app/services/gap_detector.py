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

        # ---- 3. evidence-pattern shortfall ----
        # Items in INFERRED_LOW_CONFIDENCE didn't meet their expected
        # evidence pattern (a Division 1 item lacking spec, or a material
        # item lacking the bilateral pair, etc.). The evidence_pattern
        # module handles WHY an item lands in LOW — this just surfaces
        # them. Group by (division, section) so a section with 80 weak
        # items raises ONE gap, not 80.
        from collections import defaultdict

        unilateral_items = [
            i for i in items if i.evidence_tier == "INFERRED_LOW_CONFIDENCE"
        ]
        by_section: dict[tuple[str, str], list] = defaultdict(list)
        for item in unilateral_items:
            by_section[(item.csi_division, item.csi_code)].append(item)

        def _expected_label(item) -> str:
            tc = item.trust_components or {}
            return tc.get("expected_pattern", "bilateral")

        for (div_code, section_code), group in by_section.items():
            if len(group) == 1:
                item = group[0]
                description = (
                    f"Item did not meet its expected evidence pattern "
                    f"({_expected_label(item)}). "
                    f"Description: {item.description[:140]}"
                )
                related_id = item.id
            else:
                # Group: report the dominant expected pattern for context
                from collections import Counter as _Counter
                top_expected = _Counter(
                    _expected_label(i) for i in group
                ).most_common(1)[0][0]
                description = (
                    f"{len(group)} items in section {section_code} did not "
                    f"meet their expected evidence pattern "
                    f"(mostly {top_expected}). "
                    f"Sample: {group[0].description[:120]}"
                )
                related_id = None  # No single related item

            db.add(
                Gap(
                    project_id=project_id,
                    run_id=run_id,
                    gap_type="unilateral_evidence",
                    csi_division=div_code,
                    csi_section=section_code,
                    description=description,
                    severity="info",
                    suggested_remediation=(
                        f"{len(group)} item(s) — verify each in the "
                        f"low-confidence queue; either accept (mark as "
                        f"covered), reject, or promote to RFI for the "
                        f"design team."
                    ),
                    related_item_id=related_id,
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
