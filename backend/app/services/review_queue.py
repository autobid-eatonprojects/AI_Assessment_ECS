"""Stage 7 — HITL review queue queries + mutations.

Read side:
    list_conflicts, list_gaps, list_low_confidence — all run-scoped to the
    *latest* run for a project (older runs stay in DB but the queue is
    always on the current state).

Write side:
    resolve_conflict, acknowledge_gap, reclassify_item, move_item_package
    — every call writes an AuditLog row tagged ``actor='user:<email>'``.

The router layer (api/review.py, api/packages.py) wraps these and handles
HTTP status codes / 404s.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    Conflict,
    ConflictMember,
    Gap,
    ScopeExtractionRun,
    ScopeItem,
    TradePackage,
    TradePackageItem,
)
from .audit import record_audit
from .csi_grounder import ground_code
from .trade_list_parser import get_taxonomy_for_project

log = logging.getLogger(__name__)


async def latest_run_id(db: AsyncSession, project_id: str) -> str | None:
    row = (
        await db.execute(
            select(ScopeExtractionRun.id)
            .where(ScopeExtractionRun.project_id == project_id)
            .order_by(ScopeExtractionRun.started_at.desc())
            .limit(1)
        )
    ).first()
    return row[0] if row else None


# -----------------------------------------------------------------------------
# Conflicts
# -----------------------------------------------------------------------------


async def list_conflicts(
    db: AsyncSession,
    project_id: str,
    status_filter: str | None = "open",
) -> tuple[list[Conflict], dict[str, ScopeItem]]:
    """Return (conflicts, item_snapshot_by_id) for the latest run."""
    run_id = await latest_run_id(db, project_id)
    if run_id is None:
        return [], {}

    q = (
        select(Conflict)
        .options(selectinload(Conflict.members))
        .where(Conflict.run_id == run_id)
        .order_by(Conflict.created_at.desc())
    )
    if status_filter:
        q = q.where(Conflict.status == status_filter)
    conflicts = (await db.execute(q)).scalars().all()
    if not conflicts:
        return [], {}

    item_ids: set[str] = set()
    for c in conflicts:
        for m in c.members:
            item_ids.add(m.scope_item_id)
    items = (
        await db.execute(select(ScopeItem).where(ScopeItem.id.in_(item_ids)))
    ).scalars().all()
    return list(conflicts), {i.id: i for i in items}


async def resolve_conflict(
    db: AsyncSession,
    *,
    project_id: str,
    conflict_id: str,
    winner_member_id: str,
    actor: str,
    note: str | None = None,
) -> Conflict:
    """Operator picks a winner among the conflict members.

    Marks the winner ConflictMember.is_winner=True, marks losers' ScopeItems
    as verifier_status='rejected', flips conflict.status='resolved', and
    writes an AuditLog row.
    """
    conflict = await db.get(Conflict, conflict_id)
    if conflict is None or conflict.project_id != project_id:
        raise LookupError(f"conflict {conflict_id} not found in project {project_id}")
    if conflict.status != "open":
        raise ValueError(f"conflict {conflict_id} is already {conflict.status}")

    members = (
        await db.execute(
            select(ConflictMember).where(ConflictMember.conflict_id == conflict_id)
        )
    ).scalars().all()
    if winner_member_id not in {m.id for m in members}:
        raise LookupError(
            f"member {winner_member_id} not part of conflict {conflict_id}"
        )

    winner: ConflictMember | None = None
    for m in members:
        m.is_winner = m.id == winner_member_id
        if m.id == winner_member_id:
            winner = m
        else:
            loser = await db.get(ScopeItem, m.scope_item_id)
            if loser is not None and loser.verifier_status != "rejected":
                loser.verifier_status = "rejected"
                loser.verifier_review = {
                    **(loser.verifier_review or {}),
                    "rejected_by": "operator_via_conflict",
                    "conflict_id": conflict_id,
                    "winner_member_id": winner_member_id,
                    "note": note,
                }

    conflict.status = "resolved"
    conflict.arbitrator = "operator"
    conflict.arbitration_reasoning = note or "Operator-resolved via review queue."
    conflict.arbitrated_value = {
        "winner_scope_item_id": winner.scope_item_id if winner else None,
        "winner_member_id": winner_member_id,
    }
    conflict.resolved_at = datetime.now(timezone.utc)

    await record_audit(
        db,
        project_id=project_id,
        run_id=conflict.run_id,
        action="resolve",
        entity_type="conflict",
        entity_id=conflict_id,
        actor=actor,
        payload={
            "conflict_type": conflict.conflict_type,
            "winner_member_id": winner_member_id,
            "winner_scope_item_id": winner.scope_item_id if winner else None,
        },
        note=note,
    )
    await db.commit()
    return conflict


# -----------------------------------------------------------------------------
# Gaps
# -----------------------------------------------------------------------------


async def list_gaps(
    db: AsyncSession,
    project_id: str,
    *,
    status_filter: str | None = "open",
    severity_filter: Iterable[str] | None = None,
) -> list[Gap]:
    run_id = await latest_run_id(db, project_id)
    if run_id is None:
        return []
    q = (
        select(Gap)
        .where(Gap.run_id == run_id)
        .order_by(Gap.severity, Gap.created_at.desc())
    )
    if status_filter:
        q = q.where(Gap.status == status_filter)
    if severity_filter:
        q = q.where(Gap.severity.in_(list(severity_filter)))
    return (await db.execute(q)).scalars().all()


async def acknowledge_gap(
    db: AsyncSession,
    *,
    project_id: str,
    gap_id: str,
    actor: str,
    note: str | None = None,
) -> Gap:
    gap = await db.get(Gap, gap_id)
    if gap is None or gap.project_id != project_id:
        raise LookupError(f"gap {gap_id} not found in project {project_id}")
    gap.status = "acknowledged"
    gap.acknowledged_at = datetime.now(timezone.utc)

    await record_audit(
        db,
        project_id=project_id,
        run_id=gap.run_id,
        action="acknowledge",
        entity_type="gap",
        entity_id=gap_id,
        actor=actor,
        payload={"gap_type": gap.gap_type, "severity": gap.severity},
        note=note,
    )
    await db.commit()
    return gap


# -----------------------------------------------------------------------------
# Low-confidence items
# -----------------------------------------------------------------------------


async def list_low_confidence(
    db: AsyncSession, project_id: str
) -> list[ScopeItem]:
    """ScopeItems flagged INFERRED_LOW_CONFIDENCE in the latest run."""
    run_id = await latest_run_id(db, project_id)
    if run_id is None:
        return []
    return (
        await db.execute(
            select(ScopeItem)
            .options(selectinload(ScopeItem.citations))
            .where(ScopeItem.run_id == run_id)
            .where(ScopeItem.evidence_tier == "INFERRED_LOW_CONFIDENCE")
            # NULL verifier_status means "not flagged for rejection" — must
            # include those alongside explicit non-rejected values.
            .where(
                or_(
                    ScopeItem.verifier_status.is_(None),
                    ScopeItem.verifier_status != "rejected",
                )
            )
            .order_by(ScopeItem.csi_code)
        )
    ).scalars().all()


# -----------------------------------------------------------------------------
# Item reclassification (move between CSI codes)
# -----------------------------------------------------------------------------


async def reclassify_item(
    db: AsyncSession,
    *,
    project_id: str,
    item_id: str,
    new_csi_code: str,
    actor: str,
    note: str | None = None,
) -> ScopeItem:
    item = await db.get(ScopeItem, item_id)
    if item is None or item.project_id != project_id:
        raise LookupError(f"item {item_id} not found in project {project_id}")

    taxonomy = await get_taxonomy_for_project(project_id)
    if taxonomy is None:
        raise ValueError(
            "no Trade_List.xlsx uploaded — cannot ground new CSI code"
        )
    grounded = ground_code(new_csi_code, taxonomy)
    section = taxonomy.get_section(grounded.code)
    division = taxonomy.get_division(grounded.division)

    before = {
        "csi_code": item.csi_code,
        "csi_division": item.csi_division,
        "section_title": item.section_title,
    }
    item.csi_code = grounded.code
    item.csi_division = grounded.division
    item.division_label = division.label if division else item.division_label
    item.section_title = section.title if section else None

    after = {
        "csi_code": item.csi_code,
        "csi_division": item.csi_division,
        "section_title": item.section_title,
    }

    await record_audit(
        db,
        project_id=project_id,
        run_id=item.run_id,
        action="reclassify",
        entity_type="scope_item",
        entity_id=item_id,
        actor=actor,
        payload={
            "before": before,
            "after": after,
            "ground_method": grounded.method,
            "ground_note": grounded.note,
        },
        note=note,
    )
    await db.commit()
    return item


# -----------------------------------------------------------------------------
# Trade package — manual item move
# -----------------------------------------------------------------------------


async def move_item_to_package(
    db: AsyncSession,
    *,
    project_id: str,
    item_id: str,
    target_package_id: str,
    actor: str,
    note: str | None = None,
) -> ScopeItem:
    item = await db.get(ScopeItem, item_id)
    if item is None or item.project_id != project_id:
        raise LookupError(f"item {item_id} not found in project {project_id}")
    target = await db.get(TradePackage, target_package_id)
    if target is None or target.project_id != project_id:
        raise LookupError(f"package {target_package_id} not found in project {project_id}")

    before_pkg = item.package_id

    # Remove existing TradePackageItem rows for this item
    existing = (
        await db.execute(
            select(TradePackageItem).where(TradePackageItem.scope_item_id == item_id)
        )
    ).scalars().all()
    for tpi in existing:
        await db.delete(tpi)
    await db.flush()

    # Add new mapping
    db.add(
        TradePackageItem(
            package_id=target_package_id,
            scope_item_id=item_id,
            assignment_method="manual_override",
        )
    )
    item.package_id = target_package_id

    # Refresh denormalized item_count on both packages so the UI reflects
    # the move without a separate refresh.
    if before_pkg and before_pkg != target_package_id:
        prev = await db.get(TradePackage, before_pkg)
        if prev is not None and prev.item_count > 0:
            prev.item_count -= 1
    target.item_count += 1

    await record_audit(
        db,
        project_id=project_id,
        run_id=item.run_id,
        action="override",
        entity_type="trade_package_item",
        entity_id=item_id,
        actor=actor,
        payload={
            "before_package_id": before_pkg,
            "after_package_id": target_package_id,
        },
        note=note,
    )
    await db.commit()
    return item


# -----------------------------------------------------------------------------
# Audit log
# -----------------------------------------------------------------------------


async def list_audit_log(
    db: AsyncSession,
    project_id: str,
    *,
    entity_type: str | None = None,
    limit: int = 100,
):
    from ..models import AuditLog

    q = (
        select(AuditLog)
        .where(AuditLog.project_id == project_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    return (await db.execute(q)).scalars().all()
