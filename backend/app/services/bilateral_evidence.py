"""Stage 2 — evidence-pattern coverage + tier classification.

After persistence (and after the Stage 3 link judge once it lands), every
ScopeItem in a run gets:

    bilateral_evidence : bool
        True iff there is at least one citation with evidence_type='drawing'
        AND at least one citation with evidence_type='spec'. Kept as a
        sub-signal regardless of expected pattern.

    evidence_tier : enum
        EXPLICITLY_CITED          — pattern_match=True AND confidence ≥ 0.85
                                    AND link-judge passes (Stage 3 onwards).
        INFERRED_HIGH_CONFIDENCE  — strong support but pattern not met, or
                                    pattern met with weaker confidence.
        INFERRED_LOW_CONFIDENCE   — surfaces in HITL low-confidence queue.

KEY CHANGE FROM v1:
The tier no longer demands `bilateral=True` for the top tier. It demands
`pattern_match=True`, where pattern_match is computed against the item's
EXPECTED evidence pattern (from `evidence_pattern.expected_pattern()`).
A Division 1 item like "Submittal procedures" expects spec_only; if it
has a spec citation, that's a top-tier item — not a unilateral red flag.
A material/equipment item like "Cast-in-place concrete" expects bilateral;
spec-only is a real gap and lands in INFERRED_HIGH at best.

The tier rule is intentionally tunable. We persist the per-item rationale
into ``ScopeItem.trust_components`` so the UI can explain why an item landed
in its tier without re-deriving the logic.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select

from ..database import SessionLocal
from ..models import ScopeCitation, ScopeItem
from .evidence_pattern import expected_pattern, matches_expected

log = logging.getLogger(__name__)


# Tier name constants — keep in sync with frontend EvidenceTierBadge.
EXPLICITLY_CITED = "EXPLICITLY_CITED"
INFERRED_HIGH = "INFERRED_HIGH_CONFIDENCE"
INFERRED_LOW = "INFERRED_LOW_CONFIDENCE"

# Confidence threshold for the EXPLICITLY_CITED top tier. Items below this
# but with bilateral evidence still land in INFERRED_HIGH_CONFIDENCE.
CONFIDENCE_THRESHOLD = 0.85


def _classify(
    *,
    pattern_match: bool,
    expected: str,
    bilateral: bool,
    confidence: float,
    link_judge_pass: bool | None,
    has_strong_citation: bool,
) -> tuple[str, dict]:
    """Return (tier, rationale_dict) for a single item.

    The top tier now requires `pattern_match=True` (the item's evidence
    matches its expected pattern), not raw `bilateral=True`. A spec_only
    item with a clean spec citation is top-tier; a bilateral_expected
    item missing one side is at best INFERRED_HIGH.
    """
    link_judge_state = (
        "n/a" if link_judge_pass is None else ("pass" if link_judge_pass else "fail")
    )
    base_rationale = {
        "expected_pattern": expected,
        "pattern_match": pattern_match,
        "bilateral": bilateral,
        "confidence": confidence,
        "link_judge": link_judge_state,
    }

    # Top tier: pattern matches + high confidence + link-judge isn't an
    # explicit fail. (link_judge "n/a" treated as neutral so Stage 2 can
    # ship without Stage 3 actually having run.)
    if (
        pattern_match
        and confidence >= CONFIDENCE_THRESHOLD
        and link_judge_pass is not False
    ):
        reason = {
            "spec_only": "spec citation present (drawing not expected)",
            "drawing_only": "drawing citation present (spec not expected)",
            "bilateral": "bilateral evidence + high confidence",
        }[expected]
        return EXPLICITLY_CITED, {**base_rationale, "tier_reason": reason}

    # High tier: pattern matches at lower confidence, OR doesn't match
    # but has a strong single citation that crosses the confidence bar.
    if pattern_match or (
        confidence >= CONFIDENCE_THRESHOLD and has_strong_citation
    ):
        reason = (
            f"pattern matched ({expected}, lower confidence)"
            if pattern_match
            else "strong single-side citation; pattern not fully met"
        )
        return INFERRED_HIGH, {**base_rationale, "tier_reason": reason}

    return INFERRED_LOW, {
        **base_rationale,
        "tier_reason": (
            f"expected {expected}; evidence missing or weak"
        ),
    }


async def classify_run(run_id: str) -> dict[str, int]:
    """Classify every ScopeItem in a run. Returns counts per tier.

    Persists ``bilateral_evidence``, ``evidence_tier``, and
    ``trust_components`` on each ScopeItem.

    Project-aware: if the project has no written-spec doc uploaded at
    all, "bilateral" is redefined as having a citation to every
    evidence_type that DOES exist in the project (typically just
    'drawing'). Otherwise drawing-only projects always score 0 in the
    top tier through no fault of the extraction.
    """
    counts: dict[str, int] = defaultdict(int)

    async with SessionLocal() as db:
        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        if not items:
            log.info("bilateral_evidence: no items for run %s", run_id)
            return {}

        # Determine which evidence_types are reachable on this project.
        # A project that only uploaded drawings can never produce
        # spec-side citations — penalising every item for that is wrong.
        from ..models import Document

        project_id = items[0].project_id
        project_doc_types = set(
            (
                await db.execute(
                    select(Document.doc_type).where(
                        Document.project_id == project_id
                    )
                )
            ).scalars().all()
        )
        # Map Document.doc_type → ScopeCitation.evidence_type buckets
        # (mirrors scope_runner._evidence_type_for_doc_type).
        ev_present: set[str] = set()
        if "drawing-set" in project_doc_types:
            ev_present.add("drawing")
        if "written-spec" in project_doc_types:
            ev_present.add("spec")
        if {"bid-quote", "scope-letter"} & project_doc_types:
            ev_present.add("bid")
        # Bilateral semantically requires drawing+spec; if both don't
        # exist on the project, fall back to "all available sides".
        bilateral_required = (
            {"drawing", "spec"}
            if {"drawing", "spec"}.issubset(ev_present)
            else ev_present
        )
        log.info(
            "bilateral_evidence: project %s has doc_types=%s, "
            "bilateral requires evidence_types=%s",
            project_id, sorted(project_doc_types), sorted(bilateral_required),
        )

        # Per-DIVISION corpus awareness. The project may have a spec book
        # AND drawings overall, but for a given CSI division the spec might
        # be silent (e.g. on a community center, the spec book typically
        # has no Div 22/26/27/28 sections — MEP scope lives entirely on
        # M0.1, P0.1, E0.1, FP0.1 sheets). Items in those divisions
        # CANNOT produce spec citations; flagging them as "missing the
        # bilateral pair" is wrong by construction. Downgrade their
        # expected_pattern to drawing_only.
        from ..models import Chunk
        import re as _re

        spec_chunks_text = ""
        if "written-spec" in project_doc_types:
            spec_text_rows = (
                await db.execute(
                    select(Chunk.text)
                    .join(Document, Chunk.document_id == Document.id)
                    .where(Chunk.project_id == project_id)
                    .where(Document.doc_type == "written-spec")
                    .where(Chunk.csi_section.isnot(None))
                )
            ).scalars().all()
            spec_chunks_text = "\n".join(t for t in spec_text_rows if t)

        # A division has spec evidence iff its DIVISION/SECTION header
        # appears in the spec text. Mirrors trade_filter._gather_corpus_evidence.
        divs_with_spec_evidence: set[str] = set()
        if spec_chunks_text:
            for m in _re.finditer(
                r"\b(?:DIVISION\s+0*(\d{1,2})|SECTION\s+(\d{2})\s\d{2}\s\d{2})\b",
                spec_chunks_text,
                _re.IGNORECASE,
            ):
                div = (m.group(1) or m.group(2) or "").zfill(2)
                if div:
                    divs_with_spec_evidence.add(div)

        # Drawing evidence is reachable for any division where a drawing
        # sheet maps via discipline_config (every project with drawings
        # touches all main disciplines).
        divs_with_drawing_evidence: set[str] = set()
        if "drawing-set" in project_doc_types:
            from .trade_list_parser import get_taxonomy_for_project as _get_tax
            tax = await _get_tax(project_id)
            if tax:
                divs_with_drawing_evidence = {d.code for d in tax.divisions}

        log.info(
            "bilateral_evidence: divs with spec evidence: %s",
            sorted(divs_with_spec_evidence),
        )

        item_ids = [i.id for i in items]
        cit_rows = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_(item_ids)
                )
            )
        ).scalars().all()

        # Per-item: set of evidence_type values, link-judge pass count,
        # max rerank score (proxy for citation strength).
        ev_types_by_item: dict[str, set[str]] = defaultdict(set)
        link_pass_by_item: dict[str, list[bool | None]] = defaultdict(list)
        max_rerank_by_item: dict[str, float] = defaultdict(float)
        for c in cit_rows:
            if c.evidence_type:
                ev_types_by_item[c.scope_item_id].add(c.evidence_type)
            link_pass_by_item[c.scope_item_id].append(c.is_link_judge_pass)
            if c.rerank_score is not None and c.rerank_score > max_rerank_by_item[c.scope_item_id]:
                max_rerank_by_item[c.scope_item_id] = c.rerank_score

        for item in items:
            ev_types = ev_types_by_item.get(item.id, set())
            has_spec = "spec" in ev_types
            has_drawing = "drawing" in ev_types
            # Raw bilateral: true iff both sides have at least one citation.
            # Kept as a sub-signal for transparency; tier no longer requires
            # this directly.
            bilateral = bool(
                bilateral_required
                and bilateral_required.issubset(ev_types)
            )

            # Expected evidence pattern for this item. Two-stage downgrade:
            #
            # 1. Project-level: if the project has only drawings (no spec)
            #    or only spec (no drawings), bilateral is impossible by
            #    definition. Downgrade to whichever side is available.
            #
            # 2. Per-division: even when the project has both, this
            #    division might be one-sided. On a community-center bid
            #    the spec book typically has zero Div 22/26/27/28 content;
            #    MEP scope lives entirely on the M*/P*/E*/FP* sheets. An
            #    item in those divisions CANNOT produce a spec citation
            #    by construction. Flagging those items as "missing the
            #    bilateral pair" is wrong; they correctly expect
            #    drawing_only. Same logic in the other direction for
            #    admin sections that exist only in the spec.
            expected = expected_pattern(item.csi_code, item.description)
            div = (item.csi_division or "").strip()
            if expected == "bilateral":
                # Stage 1: project-level
                if not {"spec", "drawing"}.issubset(ev_present):
                    if "drawing" in ev_present and "spec" not in ev_present:
                        expected = "drawing_only"
                    elif "spec" in ev_present and "drawing" not in ev_present:
                        expected = "spec_only"
                # Stage 2: per-division
                elif div:
                    div_has_spec = div in divs_with_spec_evidence
                    div_has_drawing = div in divs_with_drawing_evidence
                    if div_has_drawing and not div_has_spec:
                        expected = "drawing_only"
                    elif div_has_spec and not div_has_drawing:
                        expected = "spec_only"
            pattern_match = matches_expected(expected, has_spec, has_drawing)

            # link_judge_pass: True iff at least one citation passed and
            # none failed; False if any citation failed; None if not yet run.
            link_pass_states = link_pass_by_item.get(item.id, [])
            non_null = [v for v in link_pass_states if v is not None]
            link_judge_pass: bool | None
            if not non_null:
                link_judge_pass = None
            elif all(v for v in non_null):
                link_judge_pass = True
            else:
                # Mixed or all-fail — treat as fail to keep top tier strict.
                link_judge_pass = False

            # "strong citation" = a rerank score above 0.5 (Cohere rerank
            # range is 0..1, where >0.5 typically indicates relevance).
            has_strong_citation = max_rerank_by_item.get(item.id, 0.0) >= 0.5

            tier, rationale = _classify(
                pattern_match=pattern_match,
                expected=expected,
                bilateral=bilateral,
                confidence=item.confidence,
                link_judge_pass=link_judge_pass,
                has_strong_citation=has_strong_citation,
            )

            item.bilateral_evidence = bilateral
            item.evidence_tier = tier
            item.trust_components = rationale
            counts[tier] += 1

        await db.commit()

    log.info(
        "bilateral_evidence: run %s — %s",
        run_id,
        ", ".join(f"{tier}={n}" for tier, n in counts.items()),
    )
    return dict(counts)
