from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..models import Project
from ..schemas import ProjectCreate, ProjectOut, ProjectUpdate
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects", tags=["projects"])


def _to_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        document_count=len(project.documents),
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


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, db: DB, _: CurrentUser) -> None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    await db.delete(project)
    await db.commit()
