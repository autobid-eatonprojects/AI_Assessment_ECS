"""Phase 4.1 endpoints — Project Profile."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..models import Project, ProjectProfile
from ..schemas import ProjectProfileOut
from ..services.project_profiler import (
    ProfilerUnavailable,
    upsert_project_profile,
)
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/profile", tags=["profile"])


@router.get("", response_model=ProjectProfileOut | None)
async def get_profile(
    project_id: str, db: DB, _: CurrentUser
) -> ProjectProfileOut | None:
    proj = await db.execute(select(Project).where(Project.id == project_id))
    if proj.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    result = await db.execute(
        select(ProjectProfile).where(ProjectProfile.project_id == project_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return ProjectProfileOut.model_validate(row)


@router.post("", response_model=ProjectProfileOut)
async def run_profiler(
    project_id: str, db: DB, _: CurrentUser
) -> ProjectProfileOut:
    proj = await db.execute(select(Project).where(Project.id == project_id))
    if proj.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    try:
        row = await upsert_project_profile(project_id)
    except ProfilerUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"profiler failed: {e}",
        ) from e
    return ProjectProfileOut.model_validate(row)
