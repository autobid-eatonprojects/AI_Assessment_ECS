"""Stage 3 — Conflict resolution.

Cohesive operation: find disagreeing scope items in a run and resolve what
can be auto-resolved; defer the rest to HITL.

Internally three sub-operations:
  1. Detect — embedding clustering produces candidate Conflict records
     (within-CSI qty/unit mismatches; cross-division overlap)
  2. Auto-arbitrate — for clusters where every member has confidence > 0.8,
     send to Opus 4.7 and pick a winner
  3. Defer — ineligible clusters persist with status='open' for the HITL
     review queue

Two-phase commit preserved: detection commits before arbitration runs, so
if Opus fails mid-batch the surfaced Conflicts still reach HITL.

Two public surfaces (the interface IS the test surface):
  - resolve(...) — pure logic; clusters items, classifies conflicts,
        optionally arbitrates eligible clusters via Opus (routed
        through anthropic_tool_call.call_with_tool). Returns
        candidates + verdicts; no DB writes.
  - run(run_id) — DB-bound orchestrator. Loads items, calls resolve(),
        persists in two phases. This is what scope_runner calls.

HITL-side resolutions are NOT handled here — the Review API writes the
same Conflict.status state machine via PATCH; this module is purely the
system-side resolver.

Replaces the previous split of conflict_detector + conflict_arbitrator,
which were tightly-coupled siblings sharing implicit schema assumptions.
The split was vestigial — only one caller (scope_runner) ran them
sequentially, and the boundary between detect/arbitrate provided no
leverage to any external caller.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from ..database import SessionLocal
from ..models import (
    Chunk,
    Conflict,
    ConflictMember,
    ScopeCitation,
    ScopeItem,
)
from .anthropic_tool_call import call_with_tool
from .audit import record_audit
from .evidence_pattern import is_admin_description
from .scope_deduper import cluster_by_similarity

log = logging.getLogger(__name__)


# ============================================================================
# Configuration — single tuning surface
# ============================================================================

# Embedding-similarity thresholds for clustering.
WITHIN_CSI_THRESHOLD = 0.92
CROSS_DIVISION_THRESHOLD = 0.85

# Within a same-CSI-code cluster, qty values that differ by >5% trigger a
# qty_mismatch conflict. Pure clustering wouldn't catch this — items with
# similar descriptions but different numerics get merged by scope_deduper;
# we want them flagged here instead.
QTY_DISAGREEMENT_RATIO = 0.05

# Auto-arbitrate only when ALL cluster members have validator confidence
# above this threshold. Lower-confidence clusters defer to HITL because
# Opus arbitration on weak inputs amplifies the wrong answer.
# History: 0.6 → 0.8 as the system matured.
AUTO_ARBITRATE_CONFIDENCE = 0.8

_ARBITRATOR_MODEL = "claude-opus-4-7"
_ARBITRATOR_CONCURRENCY = 4

_NUMERIC_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


# ============================================================================
# Tool schema for Opus arbitration
# ============================================================================

_ARBITRATE_TOOL = {
    "name": "arbitrate_conflict",
    "description": (
        "Pick the winning member of a scope-item conflict cluster. The "
        "winner is the member whose claim is best supported by the source "
        "chunks. Return the winner's scope_item_id and a one-sentence "
        "explanation. If genuinely cannot decide, set winner_id to null."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "winner_id": {
                "type": ["string", "null"],
                "description": "scope_item_id of the winning member, or null if undecided.",
            },
            "winning_qty": {
                "type": ["string", "null"],
                "description": "Quantity to use after resolution (winner's, possibly normalized).",
            },
            "winning_unit": {
                "type": ["string", "null"],
                "description": "Unit code to use after resolution.",
            },
            "reasoning": {
                "type": "string",
                "description": "1-2 sentence explanation of why this member wins.",
            },
        },
        "required": ["winner_id", "reasoning"],
    },
}


# ============================================================================
# Result types
# ============================================================================


@dataclass
class _CandidateConflict:
    """A detected-but-not-yet-persisted conflict cluster.

    Used as the carry-state between resolve()'s clustering output and
    persistence in run(). `index` lets verdicts pair back to a specific
    candidate without depending on Python object identity.
    """
    index: int
    conflict_type: str            # "qty_mismatch" | "unit_mismatch" | "cross_division_overlap"
    csi_division: str | None
    members: list[ScopeItem]
    eligible_for_auto: bool


@dataclass
class _ArbitrationVerdict:
    """Opus's decision on one candidate conflict (`candidate_index`-paired)."""
    candidate_index: int
    winner_id: str | None
    winning_qty: str | None
    winning_unit: str | None
    reasoning: str
    cost_usd: float


@dataclass
class ResolveResult:
    """Public output of run() — what scope_runner logs and aggregates."""
    qty_mismatch: int = 0
    unit_mismatch: int = 0
    cross_division: int = 0
    arbitrated: int = 0
    deferred: int = 0
    cost_usd: float = 0.0
    resolved_conflict_ids: list[str] = field(default_factory=list)
    deferred_conflict_ids: list[str] = field(default_factory=list)


# ============================================================================
# Private helpers — detection
# ============================================================================


def _parse_qty(qty: str | None) -> float | None:
    """Pull the first numeric out of a quantity string. ``5,200`` → 5200.0."""
    if not qty:
        return None
    m = _NUMERIC_RE.search(qty)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _qty_spread(values: list[float]) -> float | None:
    """Relative spread (max - min) / max for ≥2 numerics. None when undefined."""
    nums = [v for v in values if v is not None and v > 0]
    if len(nums) < 2:
        return None
    if min(nums) == max(nums):
        return 0.0
    return (max(nums) - min(nums)) / max(nums)


def _classify_within_cluster(items: list[ScopeItem]) -> str | None:
    """For a same-csi-code cluster: 'qty_mismatch' | 'unit_mismatch' | None."""
    if len(items) < 2:
        return None
    units = {(i.unit or "").strip().lower() for i in items if i.unit}
    if len(units) > 1:
        return "unit_mismatch"
    qty_values = [_parse_qty(i.quantity) for i in items]
    spread = _qty_spread([v for v in qty_values if v is not None])
    if spread is not None and spread > QTY_DISAGREEMENT_RATIO:
        return "qty_mismatch"
    return None


# ============================================================================
# Private helpers — arbitration eligibility + Opus prompt
# ============================================================================


def _eligible_for_auto(items: list[ScopeItem]) -> bool:
    """Auto-arbitrate only when every member has confidence > threshold."""
    if not items:
        return False
    return all(
        (i.confidence is not None) and (i.confidence > AUTO_ARBITRATE_CONFIDENCE)
        for i in items
    )


def _is_admin_item(item: ScopeItem) -> bool:
    """True if the item is admin/QC/closeout scope.

    Admin scope is INHERENTLY per-division — a "Product data submittal"
    in Div 09 is for gypsum board; the one in Div 07 is for vapor barrier.
    They share boilerplate phrasing (cosine ~0.85+) but are different items.
    Cross-division clustering of admin items is pure noise — inflated the
    Conflicts queue with ~70% of cases that always defer to HITL.

    Two signals, either sufficient:
      - extraction_method tagged by section_extractor as admin/qc
      - description contains admin keywords (catches discipline_agent items
        and schedule_miner items that section_extractor's classifier missed)
    """
    method = (item.extraction_method or "").lower()
    if "section_extractor/admin" in method or "section_extractor/qc" in method:
        return True
    return is_admin_description(item.description)


def _format_chunks(chunks: list[Chunk]) -> str:
    parts: list[str] = []
    for c in chunks:
        meta = c.extra or {}
        sheet = meta.get("sheet_number") or "—"
        page = c.page_number or "—"
        parts.append(
            f"[chunk_id={c.id} type={c.chunk_type} sheet={sheet} page={page}]\n"
            f"{c.text or ''}"
        )
    return "\n\n---\n\n".join(parts)


def _build_prompt(
    candidate: _CandidateConflict,
    chunks_by_item: dict[str, list[Chunk]],
) -> str:
    members_block: list[str] = []
    for idx, item in enumerate(candidate.members, 1):
        chunks = chunks_by_item.get(item.id, [])
        members_block.append(
            f"--- Member {idx} ---\n"
            f"scope_item_id: {item.id}\n"
            f"CSI code:      {item.csi_code} ({item.section_title or item.division_label})\n"
            f"Description:   {item.description}\n"
            f"Specification: {item.specification or '(none)'}\n"
            f"Quantity:      {item.quantity or '(none)'} {item.unit or ''}\n"
            f"Validator confidence: {item.confidence:.2f}\n"
            f"Citations:\n{_format_chunks(chunks)}"
        )
    body = "\n\n".join(members_block)
    return (
        f"You are a senior estimator arbitrating a scope-item conflict. "
        f"The members below are the same physical scope item according to "
        f"clustering, but they disagree on at least one of: quantity, unit, "
        f"or CSI code. Read the full citations and decide which member is "
        f"best supported by the evidence.\n\n"
        f"=== CONFLICT TYPE: {candidate.conflict_type} ===\n\n"
        f"=== MEMBERS ({len(candidate.members)}) ===\n{body}\n\n"
        f"=== TASK ===\n"
        f"Use arbitrate_conflict to return the winning scope_item_id with "
        f"reasoning. Set winner_id=null only if the evidence is genuinely "
        f"too thin to decide; in that case the conflict will be sent to "
        f"human review."
    )


async def _arbitrate_one(
    candidate: _CandidateConflict,
    chunks_by_item: dict[str, list[Chunk]],
    project_id: str,
) -> _ArbitrationVerdict:
    """Run a single Opus call and return the parsed verdict + cost.

    Failures (network, rate limit, malformed output) yield a verdict with
    winner_id=None so the conflict naturally falls through to HITL.
    """
    try:
        result = await call_with_tool(
            model=_ARBITRATOR_MODEL,
            tool_def=_ARBITRATE_TOOL,
            max_tokens=1024,
            purpose="conflict-arbitrate",
            project_id=project_id,
            user_content=_build_prompt(candidate, chunks_by_item),
        )
        payload = result.parsed_input
        return _ArbitrationVerdict(
            candidate_index=candidate.index,
            winner_id=payload.get("winner_id"),
            winning_qty=payload.get("winning_qty"),
            winning_unit=payload.get("winning_unit"),
            reasoning=payload.get("reasoning") or "",
            cost_usd=result.cost_usd,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "conflict_resolution: arbitration failed on candidate %s: %s",
            candidate.index, e,
        )
        return _ArbitrationVerdict(
            candidate_index=candidate.index,
            winner_id=None,
            winning_qty=None,
            winning_unit=None,
            reasoning=f"arbitration error: {type(e).__name__}",
            cost_usd=0.0,
        )


# ============================================================================
# Public surface 1 — pure logic
# ============================================================================


async def resolve(
    items: list[ScopeItem],
    *,
    chunks_by_item: dict[str, list[Chunk]] | None = None,
    arbitrate: bool = False,
    project_id: str | None = None,
) -> tuple[list[_CandidateConflict], list[_ArbitrationVerdict]]:
    """Pure resolution logic — cluster items, classify, optionally arbitrate.

    No DB writes here. Caller (run() or a test) persists the returned
    candidates + verdicts.

    Parameters
    ----------
    items
        ScopeItem rows to consider. Must already be loaded by the caller.
    chunks_by_item
        Cited chunks per item id, used in arbitration prompts. Required
        when `arbitrate=True`; ignored otherwise.
    arbitrate
        When True, eligible candidates are sent to Opus via
        `anthropic_tool_call.call_with_tool` for auto-arbitration.
        When False, every candidate defers to HITL.
    project_id
        Required for cost-logging when `arbitrate=True`.

    Returns
    -------
    (candidates, verdicts) — `candidates` is the full set including
    deferred-for-HITL ones; `verdicts` covers only the eligible subset
    that was sent to Opus.
    """
    candidates: list[_CandidateConflict] = []
    if not items:
        return candidates, []

    descriptions = [i.description for i in items]
    csi_keys = [i.csi_code for i in items]

    # ---- Detection pass 1: within-CSI-code disagreements ----
    within_clusters = await cluster_by_similarity(
        descriptions, threshold=WITHIN_CSI_THRESHOLD, group_keys=csi_keys,
    )
    next_index = 0
    for cluster_idxs in within_clusters:
        if len(cluster_idxs) < 2:
            continue
        members = [items[i] for i in cluster_idxs]
        ctype = _classify_within_cluster(members)
        if ctype is None:
            continue
        candidates.append(_CandidateConflict(
            index=next_index,
            conflict_type=ctype,
            csi_division=members[0].csi_division,
            members=members,
            eligible_for_auto=_eligible_for_auto(members),
        ))
        next_index += 1

    # ---- Detection pass 2: cross-division overlap ----
    # Filter out admin/QC scope BEFORE clustering. Admin items
    # ("Product data submittal for X", "Closeout submittal for Y",
    # "Quality assurance — verification of Z") share boilerplate
    # phrasing across divisions without sharing scope. Including them
    # produces ~70% noise that always defers to HITL. We preserve a
    # mapping so we can recover the original `items[]` index of each
    # filtered item from the cluster results.
    nonadmin_items: list[ScopeItem] = []
    nonadmin_to_orig: list[int] = []   # cluster-output index → items[] index
    for orig_idx, it in enumerate(items):
        if not _is_admin_item(it):
            nonadmin_items.append(it)
            nonadmin_to_orig.append(orig_idx)
    n_admin_skipped = len(items) - len(nonadmin_items)
    if n_admin_skipped:
        log.info(
            "conflict_resolution: cross-division pass skipping %d admin items",
            n_admin_skipped,
        )

    cross_descriptions = [it.description for it in nonadmin_items]
    cross_clusters = await cluster_by_similarity(
        cross_descriptions, threshold=CROSS_DIVISION_THRESHOLD, group_keys=None,
    )
    for cluster_idxs in cross_clusters:
        if len(cluster_idxs) < 2:
            continue
        # cluster_idxs are indices into nonadmin_items; resolve to original items
        members = [nonadmin_items[i] for i in cluster_idxs]
        divisions = {m.csi_division for m in members}
        if len(divisions) < 2:
            continue
        candidates.append(_CandidateConflict(
            index=next_index,
            conflict_type="cross_division_overlap",
            csi_division=None,
            members=members,
            eligible_for_auto=_eligible_for_auto(members),
        ))
        next_index += 1

    # ---- Auto-arbitrate eligible candidates via Opus ----
    verdicts: list[_ArbitrationVerdict] = []
    if arbitrate and project_id is not None:
        eligible = [c for c in candidates if c.eligible_for_auto]
        if eligible:
            sem = asyncio.Semaphore(_ARBITRATOR_CONCURRENCY)
            chunks_map = chunks_by_item or {}

            async def _one(c: _CandidateConflict) -> _ArbitrationVerdict:
                async with sem:
                    return await _arbitrate_one(c, chunks_map, project_id)

            verdicts = list(await asyncio.gather(*(_one(c) for c in eligible)))

    return candidates, verdicts


# ============================================================================
# Public surface 2 — DB-bound orchestrator
# ============================================================================


async def _load_run_inputs(
    run_id: str,
) -> tuple[
    list[ScopeItem],
    str | None,                                  # project_id
    dict[str, ScopeCitation | None],             # best citation per item
    dict[str, list[Chunk]],                      # all cited chunks per item
]:
    """Load everything resolve() needs from the DB in one batch."""
    async with SessionLocal() as db:
        items = (
            await db.execute(select(ScopeItem).where(ScopeItem.run_id == run_id))
        ).scalars().all()
        if not items:
            return [], None, {}, {}
        project_id = items[0].project_id

        cit_rows = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_([i.id for i in items])
                )
            )
        ).scalars().all()

        # Best citation per item (max rerank_score) — used as the
        # representative ConflictMember.citation_id when persisting.
        best_cit_by_item: dict[str, ScopeCitation | None] = defaultdict(lambda: None)
        for c in cit_rows:
            cur = best_cit_by_item[c.scope_item_id]
            if cur is None or (c.rerank_score or 0.0) > (cur.rerank_score or 0.0):
                best_cit_by_item[c.scope_item_id] = c

        chunk_ids = {c.chunk_id for c in cit_rows}
        chunks: list[Chunk] = []
        if chunk_ids:
            chunks = (
                await db.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
            ).scalars().all()
        chunks_by_id = {c.id: c for c in chunks}
        chunks_by_item: dict[str, list[Chunk]] = defaultdict(list)
        for c in cit_rows:
            ch = chunks_by_id.get(c.chunk_id)
            if ch is not None:
                chunks_by_item[c.scope_item_id].append(ch)

        return list(items), project_id, dict(best_cit_by_item), dict(chunks_by_item)


async def _persist_detection(
    project_id: str,
    run_id: str,
    candidates: list[_CandidateConflict],
    best_cit_by_item: dict[str, ScopeCitation | None],
) -> tuple[dict[int, str], ResolveResult]:
    """Phase 1 commit: write Conflict + ConflictMember rows for every detected
    cluster. Returns (candidate_index → conflict_id) plus rollup counts."""
    rollup = ResolveResult()
    conflict_id_by_candidate: dict[int, str] = {}
    async with SessionLocal() as db:
        for cand in candidates:
            conflict = Conflict(
                project_id=project_id,
                run_id=run_id,
                conflict_type=cand.conflict_type,
                csi_division=cand.csi_division,
                status="open",
            )
            db.add(conflict)
            await db.flush()
            conflict_id_by_candidate[cand.index] = conflict.id
            for idx, item in enumerate(cand.members):
                cit = best_cit_by_item.get(item.id)
                db.add(
                    ConflictMember(
                        conflict_id=conflict.id,
                        scope_item_id=item.id,
                        role="primary" if idx == 0 else "contradictor",
                        citation_id=cit.id if cit else None,
                    )
                )
            if cand.conflict_type == "qty_mismatch":
                rollup.qty_mismatch += 1
            elif cand.conflict_type == "unit_mismatch":
                rollup.unit_mismatch += 1
            elif cand.conflict_type == "cross_division_overlap":
                rollup.cross_division += 1
        await db.commit()
    return conflict_id_by_candidate, rollup


async def _persist_arbitration(
    project_id: str,
    run_id: str,
    candidates_by_index: dict[int, _CandidateConflict],
    conflict_id_by_candidate: dict[int, str],
    verdicts: list[_ArbitrationVerdict],
    rollup: ResolveResult,
) -> None:
    """Phase 2 commit: apply Opus verdicts to Conflict + ConflictMember +
    loser ScopeItem rows; write audit logs.

    Mutates `rollup` in place with arbitrated/deferred counts and ids."""
    if not verdicts:
        # Every detected conflict goes to HITL.
        rollup.deferred = len(candidates_by_index)
        rollup.deferred_conflict_ids = list(conflict_id_by_candidate.values())
        return

    arbitrated_ids: list[str] = []
    async with SessionLocal() as db:
        for v in verdicts:
            rollup.cost_usd += v.cost_usd
            cand = candidates_by_index.get(v.candidate_index)
            conflict_id = conflict_id_by_candidate.get(v.candidate_index)
            if cand is None or conflict_id is None:
                continue

            valid_winner = (
                v.winner_id is not None
                and v.winner_id in {m.id for m in cand.members}
            )
            if not valid_winner:
                # Model abstained or returned a bogus id — leave for HITL.
                continue

            conflict = await db.get(Conflict, conflict_id)
            if conflict is None:
                continue
            conflict.status = "resolved"
            conflict.arbitrator = "opus-verifier"
            conflict.arbitration_reasoning = v.reasoning
            conflict.arbitrated_value = {
                "winner_scope_item_id": v.winner_id,
                "winning_qty": v.winning_qty,
                "winning_unit": v.winning_unit,
            }
            conflict.resolved_at = datetime.now(timezone.utc)

            members = (
                await db.execute(
                    select(ConflictMember).where(
                        ConflictMember.conflict_id == conflict_id
                    )
                )
            ).scalars().all()
            for m in members:
                m.is_winner = (m.scope_item_id == v.winner_id)
                if m.scope_item_id != v.winner_id:
                    loser = await db.get(ScopeItem, m.scope_item_id)
                    if loser is not None and loser.verifier_status != "rejected":
                        loser.verifier_status = "rejected"
                        loser.verifier_review = {
                            **(loser.verifier_review or {}),
                            "rejected_by": "conflict_arbitrator",
                            "conflict_id": conflict_id,
                            "winner_id": v.winner_id,
                            "reasoning": v.reasoning,
                        }

            await record_audit(
                db,
                project_id=project_id,
                run_id=run_id,
                action="resolve",
                entity_type="conflict",
                entity_id=conflict_id,
                actor="system:conflict_arbitrator",
                payload={
                    "conflict_type": conflict.conflict_type,
                    "winner_id": v.winner_id,
                    # cand.members is list[ScopeItem]; use .id, not the
                    # ConflictMember.scope_item_id of the legacy code
                    "members": [m.id for m in cand.members],
                    "winning_qty": v.winning_qty,
                    "winning_unit": v.winning_unit,
                },
                note=v.reasoning[:500],
            )
            arbitrated_ids.append(conflict_id)
        await db.commit()

    rollup.arbitrated = len(arbitrated_ids)
    rollup.deferred = len(conflict_id_by_candidate) - rollup.arbitrated
    rollup.resolved_conflict_ids = arbitrated_ids
    rollup.deferred_conflict_ids = [
        cid for cid in conflict_id_by_candidate.values() if cid not in arbitrated_ids
    ]


async def run(run_id: str) -> ResolveResult:
    """Detect conflicts, auto-arbitrate eligible ones, persist outcomes.

    Two-phase commit: detection persists (Phase 1) before arbitration
    runs. If Opus calls fail mid-batch, the surfaced Conflicts still
    reach the HITL queue.
    """
    items, project_id, best_cit_by_item, chunks_by_item = await _load_run_inputs(run_id)
    if not items or project_id is None:
        return ResolveResult()

    from .anthropic_tool_call import _get_client

    arbitrate = _get_client() is not None
    if not arbitrate:
        log.warning(
            "conflict_resolution: no ANTHROPIC_API_KEY — detection only, "
            "all conflicts deferred to HITL"
        )

    candidates, verdicts = await resolve(
        items,
        chunks_by_item=chunks_by_item,
        arbitrate=arbitrate,
        project_id=project_id,
    )

    if not candidates:
        log.info("conflict_resolution: run %s — no conflicts detected", run_id)
        return ResolveResult()

    # Phase 1: persist detection. Survives an arbitration crash.
    conflict_id_by_candidate, rollup = await _persist_detection(
        project_id, run_id, candidates, best_cit_by_item,
    )

    # Phase 2: persist arbitration outcomes (if any).
    candidates_by_index = {c.index: c for c in candidates}
    await _persist_arbitration(
        project_id, run_id, candidates_by_index,
        conflict_id_by_candidate, verdicts, rollup,
    )

    log.info(
        "conflict_resolution: run %s — detected qty=%d unit=%d cross=%d, "
        "arbitrated=%d deferred=%d, $%.3f",
        run_id, rollup.qty_mismatch, rollup.unit_mismatch, rollup.cross_division,
        rollup.arbitrated, rollup.deferred, rollup.cost_usd,
    )
    return rollup
