"""Stage 3 — conflict detector.

Two clustering passes over a run's ScopeItems, both using the shared cosine
kernel from ``scope_deduper.cluster_by_similarity``:

    1. Within-CSI-code disagreements (cosine ≥ 0.92, group_keys=csi_code).
       The deduper would silently merge these. We instead surface them as
       Conflict rows when members disagree on quantity (>5% spread on
       comparable units) or unit code.

    2. Cross-division overlap (cosine ≥ 0.85, no grouping). Catches items
       like "concrete sealer" appearing under both Div 03 (Concrete) and
       Div 09 (Finishes) — same physical scope, two CSI codes. Surfaced as
       conflict_type='cross_division_overlap'.

Both pass types only emit Conflicts when the cluster has >1 member with
genuine disagreement; pure duplicates are ignored (the deduper handles them).

No LLM calls — pure embedding clustering + numeric comparison.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from ..database import SessionLocal
from ..models import Conflict, ConflictMember, ScopeCitation, ScopeItem
from .scope_deduper import cluster_by_similarity

log = logging.getLogger(__name__)


_WITHIN_CSI_THRESHOLD = 0.92
_CROSS_DIVISION_THRESHOLD = 0.85
# Per the plan: >5% spread across comparable units triggers a qty conflict.
_QTY_DISAGREEMENT_RATIO = 0.05


_NUMERIC_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


@dataclass
class _DetectorRollup:
    qty_mismatch: int = 0
    unit_mismatch: int = 0
    cross_division: int = 0


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
    """Return relative spread (max - min) / max for ≥2 numeric quantities.

    Returns None when fewer than two distinct numerics or any value ≤ 0.
    """
    nums = [v for v in values if v is not None and v > 0]
    if len(nums) < 2:
        return None
    if min(nums) == max(nums):
        return 0.0
    return (max(nums) - min(nums)) / max(nums)


def _classify_within_cluster(items: list[ScopeItem]) -> str | None:
    """For a same-csi-code cluster, return 'qty_mismatch' | 'unit_mismatch' | None."""
    if len(items) < 2:
        return None

    units = {(i.unit or "").strip().lower() for i in items if i.unit}
    if len(units) > 1:
        return "unit_mismatch"

    qty_values = [_parse_qty(i.quantity) for i in items]
    spread = _qty_spread([v for v in qty_values if v is not None])
    if spread is not None and spread > _QTY_DISAGREEMENT_RATIO:
        return "qty_mismatch"

    return None


async def _persist_conflict(
    db,
    *,
    project_id: str,
    run_id: str,
    conflict_type: str,
    csi_division: str | None,
    members: list[ScopeItem],
    citation_by_item: dict[str, ScopeCitation | None],
) -> None:
    conflict = Conflict(
        project_id=project_id,
        run_id=run_id,
        conflict_type=conflict_type,
        csi_division=csi_division,
        status="open",
    )
    db.add(conflict)
    await db.flush()

    for idx, item in enumerate(members):
        cit = citation_by_item.get(item.id)
        db.add(
            ConflictMember(
                conflict_id=conflict.id,
                scope_item_id=item.id,
                role="primary" if idx == 0 else "contradictor",
                citation_id=cit.id if cit else None,
            )
        )


async def detect_run(run_id: str) -> _DetectorRollup:
    """Detect within-CSI and cross-division conflicts for a run."""
    rollup = _DetectorRollup()

    async with SessionLocal() as db:
        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        if not items:
            return rollup
        project_id = items[0].project_id

        # One representative citation per item (best rerank score). Used so
        # ConflictMember.citation_id points at the most defensible source.
        cit_rows = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_([i.id for i in items])
                )
            )
        ).scalars().all()
        best_cit_by_item: dict[str, ScopeCitation | None] = defaultdict(lambda: None)
        for c in cit_rows:
            cur = best_cit_by_item[c.scope_item_id]
            if cur is None or (c.rerank_score or 0.0) > (cur.rerank_score or 0.0):
                best_cit_by_item[c.scope_item_id] = c

        # ---- Pass 1: within-CSI-code disagreements ----
        descriptions = [i.description for i in items]
        csi_keys = [i.csi_code for i in items]
        within_clusters = await cluster_by_similarity(
            descriptions,
            threshold=_WITHIN_CSI_THRESHOLD,
            group_keys=csi_keys,
        )
        for cluster_idxs in within_clusters:
            if len(cluster_idxs) < 2:
                continue
            members = [items[i] for i in cluster_idxs]
            ctype = _classify_within_cluster(members)
            if ctype is None:
                continue
            await _persist_conflict(
                db,
                project_id=project_id,
                run_id=run_id,
                conflict_type=ctype,
                csi_division=members[0].csi_division,
                members=members,
                citation_by_item=best_cit_by_item,
            )
            if ctype == "qty_mismatch":
                rollup.qty_mismatch += 1
            elif ctype == "unit_mismatch":
                rollup.unit_mismatch += 1

        # ---- Pass 2: cross-division overlap ----
        cross_clusters = await cluster_by_similarity(
            descriptions,
            threshold=_CROSS_DIVISION_THRESHOLD,
            group_keys=None,
        )
        for cluster_idxs in cross_clusters:
            if len(cluster_idxs) < 2:
                continue
            members = [items[i] for i in cluster_idxs]
            divisions = {m.csi_division for m in members}
            if len(divisions) < 2:
                continue  # same-division cluster — already handled above
            await _persist_conflict(
                db,
                project_id=project_id,
                run_id=run_id,
                conflict_type="cross_division_overlap",
                csi_division=None,
                members=members,
                citation_by_item=best_cit_by_item,
            )
            rollup.cross_division += 1

        await db.commit()

    log.info(
        "conflict_detector: run %s — qty_mismatch=%d unit_mismatch=%d "
        "cross_division=%d",
        run_id,
        rollup.qty_mismatch,
        rollup.unit_mismatch,
        rollup.cross_division,
    )
    return rollup
