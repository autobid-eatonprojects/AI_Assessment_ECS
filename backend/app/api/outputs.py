"""Stage 10 — Output generation endpoints (SOW Word docs + gap PDF)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from ..models import Project
from ..services import output_runner
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/outputs", tags=["outputs"])


async def _ensure_project(db, project_id: str) -> Project:
    proj = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return proj


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate(
    project_id: str,
    db: DB,
    user: CurrentUser,
    background: BackgroundTasks,
    run_id: str | None = Query(default=None),
):
    """Kick off output generation in the background.

    Returns immediately with a 202; poll ``GET /outputs`` for progress.
    """
    await _ensure_project(db, project_id)

    async def _run():
        try:
            await output_runner.generate_outputs(
                project_id, run_id=run_id, actor=f"user:{user}"
            )
        except Exception as e:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                "output generation failed for project %s: %s", project_id, e
            )

    background.add_task(_run)
    return {"status": "scheduled", "project_id": project_id, "run_id": run_id}


@router.get("")
async def list_outputs(
    project_id: str,
    db: DB,
    _: CurrentUser,
    run_id: str | None = Query(default=None),
):
    await _ensure_project(db, project_id)
    return await output_runner.list_outputs(project_id, run_id=run_id)


@router.get("/download")
async def download(
    project_id: str,
    db: DB,
    _: CurrentUser,
    run_id: str = Query(...),
    filename: str = Query(...),
):
    """Serve a generated file by run_id + filename. Path traversal guarded."""
    await _ensure_project(db, project_id)
    file_path = output_runner.output_path(project_id, run_id, filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"file not found: {filename}",
        )
    media = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if file_path.suffix == ".docx"
        else "application/pdf"
        if file_path.suffix == ".pdf"
        else "application/octet-stream"
    )
    return FileResponse(file_path, media_type=media, filename=file_path.name)
