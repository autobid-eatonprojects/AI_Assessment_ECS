"""Stage 7 — HITL review queue endpoints (Conflicts / Gaps / Low confidence)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from ..models import Project
from ..schemas import (
    ConflictItemSummary,
    ConflictMemberOut,
    ConflictOut,
    ConflictResolveIn,
    GapAcknowledgeIn,
    GapOut,
    ItemReclassifyIn,
    ScopeItemOut,
)
from ..services import review_queue
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/review", tags=["review"])


async def _ensure_project(db, project_id: str) -> Project:
    proj = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return proj


# -----------------------------------------------------------------------------
# Conflicts
# -----------------------------------------------------------------------------


@router.get("/conflicts", response_model=list[ConflictOut])
async def list_conflicts(
    project_id: str,
    db: DB,
    _: CurrentUser,
    status_filter: str | None = Query("open", alias="status"),
) -> list[ConflictOut]:
    await _ensure_project(db, project_id)
    conflicts, items_by_id = await review_queue.list_conflicts(
        db, project_id, status_filter=status_filter
    )
    out: list[ConflictOut] = []
    for c in conflicts:
        snapshots = [
            ConflictItemSummary(
                id=item.id,
                csi_code=item.csi_code,
                csi_division=item.csi_division,
                description=item.description,
                quantity=item.quantity,
                unit=item.unit,
                confidence=item.confidence,
                evidence_tier=item.evidence_tier,
            )
            for m in c.members
            if (item := items_by_id.get(m.scope_item_id))
        ]
        out.append(
            ConflictOut(
                id=c.id,
                project_id=c.project_id,
                run_id=c.run_id,
                conflict_type=c.conflict_type,
                csi_division=c.csi_division,
                status=c.status,
                arbitrated_value=c.arbitrated_value,
                arbitration_reasoning=c.arbitration_reasoning,
                arbitrator=c.arbitrator,
                created_at=c.created_at,
                resolved_at=c.resolved_at,
                members=[ConflictMemberOut.model_validate(m) for m in c.members],
                item_snapshots=snapshots,
            )
        )
    return out


@router.post("/conflicts/{conflict_id}/resolve", response_model=ConflictOut)
async def resolve_conflict(
    project_id: str,
    conflict_id: str,
    payload: ConflictResolveIn,
    db: DB,
    user: CurrentUser,
) -> ConflictOut:
    await _ensure_project(db, project_id)
    try:
        conflict = await review_queue.resolve_conflict(
            db,
            project_id=project_id,
            conflict_id=conflict_id,
            winner_member_id=payload.winner_member_id,
            actor=f"user:{user}",
            note=payload.note,
        )
    except LookupError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    # Re-hydrate with snapshots for the response
    refreshed_list, items_by_id = await review_queue.list_conflicts(
        db, project_id, status_filter=None
    )
    refreshed = next((c for c in refreshed_list if c.id == conflict_id), conflict)
    snapshots = [
        ConflictItemSummary(
            id=item.id,
            csi_code=item.csi_code,
            csi_division=item.csi_division,
            description=item.description,
            quantity=item.quantity,
            unit=item.unit,
            confidence=item.confidence,
            evidence_tier=item.evidence_tier,
        )
        for m in refreshed.members
        if (item := items_by_id.get(m.scope_item_id))
    ]
    return ConflictOut(
        id=refreshed.id,
        project_id=refreshed.project_id,
        run_id=refreshed.run_id,
        conflict_type=refreshed.conflict_type,
        csi_division=refreshed.csi_division,
        status=refreshed.status,
        arbitrated_value=refreshed.arbitrated_value,
        arbitration_reasoning=refreshed.arbitration_reasoning,
        arbitrator=refreshed.arbitrator,
        created_at=refreshed.created_at,
        resolved_at=refreshed.resolved_at,
        members=[ConflictMemberOut.model_validate(m) for m in refreshed.members],
        item_snapshots=snapshots,
    )


# -----------------------------------------------------------------------------
# Gaps
# -----------------------------------------------------------------------------


@router.get("/gaps", response_model=list[GapOut])
async def list_gaps(
    project_id: str,
    db: DB,
    _: CurrentUser,
    status_filter: str | None = Query("open", alias="status"),
    severity: list[str] | None = Query(default=None),
) -> list[GapOut]:
    await _ensure_project(db, project_id)
    gaps = await review_queue.list_gaps(
        db,
        project_id,
        status_filter=status_filter,
        severity_filter=severity,
    )
    return [GapOut.model_validate(g) for g in gaps]


@router.post("/gaps/{gap_id}/acknowledge", response_model=GapOut)
async def acknowledge_gap(
    project_id: str,
    gap_id: str,
    payload: GapAcknowledgeIn,
    db: DB,
    user: CurrentUser,
) -> GapOut:
    await _ensure_project(db, project_id)
    try:
        gap = await review_queue.acknowledge_gap(
            db,
            project_id=project_id,
            gap_id=gap_id,
            actor=f"user:{user}",
            note=payload.note,
        )
    except LookupError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    return GapOut.model_validate(gap)


# -----------------------------------------------------------------------------
# Low-confidence items
# -----------------------------------------------------------------------------


@router.get("/low-confidence", response_model=list[ScopeItemOut])
async def list_low_confidence(
    project_id: str, db: DB, _: CurrentUser
) -> list[ScopeItemOut]:
    await _ensure_project(db, project_id)
    items = await review_queue.list_low_confidence(db, project_id)
    return [ScopeItemOut.model_validate(it) for it in items]


# -----------------------------------------------------------------------------
# Item reclassification
# -----------------------------------------------------------------------------


@router.post("/items/{item_id}/reclassify", response_model=ScopeItemOut)
async def reclassify_item(
    project_id: str,
    item_id: str,
    payload: ItemReclassifyIn,
    db: DB,
    user: CurrentUser,
) -> ScopeItemOut:
    await _ensure_project(db, project_id)
    try:
        item = await review_queue.reclassify_item(
            db,
            project_id=project_id,
            item_id=item_id,
            new_csi_code=payload.new_csi_code,
            actor=f"user:{user}",
            note=payload.note,
        )
    except LookupError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e
    return ScopeItemOut.model_validate(item)
