from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..models import Project
from ..schemas import LifecycleTransition, ProjectCreate, ProjectOut, ProjectUpdate
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects", tags=["projects"])


# Allowed transitions in the project lifecycle. Source state → set of valid
# destination states. Anything else is rejected with 400.
_LIFECYCLE_TRANSITIONS: dict[str, set[str]] = {
    "setup": {"open-for-bids"},
    "open-for-bids": {"setup", "complete"},
    "complete": {"open-for-bids"},
}


def _to_out(project: Project) -> ProjectOut:
    project_doc_count = sum(1 for d in project.documents if d.source == "project_document")
    bid_count = sum(1 for d in project.documents if d.source == "bid_submission")
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        lifecycle_state=project.lifecycle_state,  # type: ignore[arg-type]
        created_at=project.created_at,
        updated_at=project.updated_at,
        document_count=len(project.documents),
        project_document_count=project_doc_count,
        bid_submission_count=bid_count,
        trust_score_latest=project.trust_score_latest,
    )


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: DB, _: CurrentUser) -> list[ProjectOut]:
    result = await db.execute(
        select(Project).options(selectinload(Project.documents)).order_by(Project.created_at.desc())
    )
    return [_to_out(p) for p in result.scalars().all()]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, db: DB, _: CurrentUser) -> ProjectOut:
    project = Project(name=payload.name, description=payload.description)
    db.add(project)
    await db.commit()
    await db.refresh(project, attribute_names=["documents"])
    return _to_out(project)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, db: DB, _: CurrentUser) -> ProjectOut:
    result = await db.execute(
        select(Project).options(selectinload(Project.documents)).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return _to_out(project)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: str, payload: ProjectUpdate, db: DB, _: CurrentUser
) -> ProjectOut:
    result = await db.execute(
        select(Project).options(selectinload(Project.documents)).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description

    await db.commit()
    await db.refresh(project, attribute_names=["documents"])
    return _to_out(project)


@router.post("/{project_id}/lifecycle", response_model=ProjectOut)
async def transition_lifecycle(
    project_id: str, payload: LifecycleTransition, db: DB, _: CurrentUser
) -> ProjectOut:
    result = await db.execute(
        select(Project).options(selectinload(Project.documents)).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    current = project.lifecycle_state
    target = payload.new_state
    if target == current:
        return _to_out(project)
    valid = _LIFECYCLE_TRANSITIONS.get(current, set())
    if target not in valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"cannot transition from '{current}' to '{target}'",
        )

    project.lifecycle_state = target
    await db.commit()
    await db.refresh(project, attribute_names=["documents"])
    return _to_out(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, db: DB, _: CurrentUser) -> None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    await db.delete(project)
    await db.commit()
