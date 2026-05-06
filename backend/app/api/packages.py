"""Stage 7 — Trade package endpoints (list / detail / move)."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..models import Project, ScopeExtractionRun, ScopeItem, TradePackage
from ..schemas import (
    PackageItemMoveIn,
    ScopeItemOut,
    TradePackageDetailOut,
    TradePackageOut,
)
from ..services import review_queue
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/packages", tags=["packages"])


async def _ensure_project(db, project_id: str) -> Project:
    proj = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return proj


async def _latest_run_id(db, project_id: str) -> str | None:
    row = (
        await db.execute(
            select(ScopeExtractionRun.id)
            .where(ScopeExtractionRun.project_id == project_id)
            .order_by(ScopeExtractionRun.started_at.desc())
            .limit(1)
        )
    ).first()
    return row[0] if row else None


@router.get("", response_model=list[TradePackageOut])
async def list_packages(
    project_id: str, db: DB, _: CurrentUser
) -> list[TradePackageOut]:
    await _ensure_project(db, project_id)
    run_id = await _latest_run_id(db, project_id)
    if run_id is None:
        return []
    rows = (
        await db.execute(
            select(TradePackage)
            .where(TradePackage.run_id == run_id)
            .order_by(TradePackage.item_count.desc())
        )
    ).scalars().all()
    return [TradePackageOut.model_validate(p) for p in rows]


@router.get("/{package_id}", response_model=TradePackageDetailOut)
async def get_package_detail(
    project_id: str, package_id: str, db: DB, _: CurrentUser
) -> TradePackageDetailOut:
    await _ensure_project(db, project_id)
    pkg = await db.get(TradePackage, package_id)
    if pkg is None or pkg.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="package not found"
        )

    items = (
        await db.execute(
            select(ScopeItem)
            .options(selectinload(ScopeItem.citations))
            .where(ScopeItem.package_id == package_id)
            .order_by(ScopeItem.csi_code)
        )
    ).scalars().all()

    by_section: dict[str, list[ScopeItem]] = defaultdict(list)
    for it in items:
        by_section[it.csi_code].append(it)
    items_by_section = [
        {
            "csi_section": section,
            "section_title": (members[0].section_title if members else None),
            "items": [ScopeItemOut.model_validate(m).model_dump() for m in members],
        }
        for section, members in sorted(by_section.items())
    ]

    return TradePackageDetailOut(
        id=pkg.id,
        project_id=pkg.project_id,
        run_id=pkg.run_id,
        package_key=pkg.package_key,
        package_label=pkg.package_label,
        csi_divisions=pkg.csi_divisions,
        bundling_rule_source=pkg.bundling_rule_source,
        item_count=pkg.item_count,
        bilateral_count=pkg.bilateral_count,
        avg_confidence=pkg.avg_confidence,
        narrative_md=pkg.narrative_md,
        created_at=pkg.created_at,
        items_by_section=items_by_section,
    )


@router.post("/{package_id}/items/{item_id}/move", response_model=ScopeItemOut)
async def move_item(
    project_id: str,
    package_id: str,
    item_id: str,
    payload: PackageItemMoveIn,
    db: DB,
    user: CurrentUser,
) -> ScopeItemOut:
    """Move ``item_id`` from its current package to ``target_package_id``.

    The path package_id is informational (to make the URL self-describing
    about which package the item is being moved *from*). The actual target
    is in the request body.
    """
    await _ensure_project(db, project_id)
    try:
        item = await review_queue.move_item_to_package(
            db,
            project_id=project_id,
            item_id=item_id,
            target_package_id=payload.target_package_id,
            actor=f"user:{user}",
            note=payload.note,
        )
    except LookupError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    return ScopeItemOut.model_validate(item)
