"""Stage 2 — bilateral evidence + tier classification.

After persistence (and after the Stage 3 link judge once it lands), every
ScopeItem in a run gets:

    bilateral_evidence : bool
        True iff there is at least one citation with evidence_type='drawing'
        AND at least one citation with evidence_type='spec'. Computed by a
        single GROUP BY on scope_citations (no JOIN through chunks needed
        thanks to the Stage 1 denormalization).

    evidence_tier : enum
        EXPLICITLY_CITED          — bilateral=True AND confidence ≥ 0.85
                                    AND link-judge passes (Stage 3 onwards).
                                    Until Stage 3 lands, the link-judge
                                    requirement is implicitly skipped.
        INFERRED_HIGH_CONFIDENCE  — strong support but missing one side, or
                                    bilateral with weaker confidence.
        INFERRED_LOW_CONFIDENCE   — surfaces in HITL low-confidence queue.

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
    bilateral: bool,
    confidence: float,
    link_judge_pass: bool | None,
    has_strong_citation: bool,
) -> tuple[str, dict]:
    """Return (tier, rationale_dict) for a single item.

    Rationale is shaped to render as a tooltip on the EvidenceTierBadge:
        {"bilateral": bool, "confidence": float, "link_judge": "n/a"|"pass"|"fail",
         "tier_reason": str}
    """
    link_judge_state = (
        "n/a" if link_judge_pass is None else ("pass" if link_judge_pass else "fail")
    )

    # Top tier requires all three signals (link-judge "n/a" treated as
    # neutral so Stage 2 can ship without Stage 3).
    if (
        bilateral
        and confidence >= CONFIDENCE_THRESHOLD
        and link_judge_pass is not False
    ):
        return EXPLICITLY_CITED, {
            "bilateral": True,
            "confidence": confidence,
            "link_judge": link_judge_state,
            "tier_reason": "bilateral evidence + high confidence",
        }

    # High tier: either bilateral OR strong-confidence-with-strong-citation.
    if bilateral or (
        confidence >= CONFIDENCE_THRESHOLD and has_strong_citation
    ):
        return INFERRED_HIGH, {
            "bilateral": bilateral,
            "confidence": confidence,
            "link_judge": link_judge_state,
            "tier_reason": (
                "bilateral evidence (lower confidence)"
                if bilateral
                else "strong single-side citation"
            ),
        }

    return INFERRED_LOW, {
        "bilateral": bilateral,
        "confidence": confidence,
        "link_judge": link_judge_state,
        "tier_reason": "one-sided evidence and/or weak validator support",
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
            # Project-aware: bilateral when the item cites every
            # evidence_type that exists on this project.
            bilateral = bool(
                bilateral_required
                and bilateral_required.issubset(ev_types)
            )

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
