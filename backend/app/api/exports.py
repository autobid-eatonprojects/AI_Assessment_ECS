"""P8 — fine-tuning dataset export endpoints.

GET /projects/{id}/exports/link-judge-dataset
    Streams the project's audit-log JSONL as a downloadable file.
    Re-generates from current AuditLog state on each request (cheap
    SQL + file I/O — no LLM cost). The file lives under
    <storage_root>/exports/<project_id>/link_judge_dataset.jsonl.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from ..models import Project
from ..services.audit_exporter import export_link_judge_dataset
from .deps import DB, CurrentUser

log = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/exports", tags=["exports"])


async def _ensure_project(db, project_id: str) -> Project:
    proj = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return proj


@router.get("/link-judge-dataset")
async def download_link_judge_dataset(
    project_id: str,
    db: DB,
    _: CurrentUser,
):
    """Generate + download the project's link-judge fine-tuning JSONL.

    Re-builds from the current AuditLog on every request so the file
    always reflects the latest operator decisions.
    """
    await _ensure_project(db, project_id)
    result = await export_link_judge_dataset(project_id)
    file_path = result["path"]
    from pathlib import Path

    p = Path(file_path)
    if not p.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="export file not found after build",
        )
    return FileResponse(
        p,
        media_type="application/x-ndjson",
        filename=f"link_judge_dataset_{project_id[:8]}.jsonl",
        headers={
            "X-Examples-Total": str(result["examples_total"]),
            "X-Examples-Written": str(result["examples_written"]),
        },
    )
