"""P9 — RFI list endpoints.

Two routes:
  POST /projects/{id}/rfi/generate?run_id=... → kick off Opus draft
  GET  /projects/{id}/rfi?run_id=...          → return cached list +
                                                 rendered Markdown
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from ..config import settings
from ..models import Project, ScopeExtractionRun
from ..services import rfi_generator
from .deps import DB, CurrentUser

log = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/rfi", tags=["rfi"])


def _rfi_cache_path(project_id: str, run_id: str) -> Path:
    return (
        Path(settings.storage_root)
        / "exports"
        / project_id
        / f"rfi_{run_id}.json"
    )


async def _ensure_project(db, project_id: str) -> Project:
    proj = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return proj


async def _resolve_run_id(db, project_id: str, run_id: str | None) -> str:
    if run_id:
        return run_id
    latest = (
        await db.execute(
            select(ScopeExtractionRun)
            .where(ScopeExtractionRun.project_id == project_id)
            .where(ScopeExtractionRun.status == "complete")
            .order_by(ScopeExtractionRun.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "no completed scope extraction run for this project — "
                "run scope extraction first"
            ),
        )
    return latest.id


@router.post("/generate")
async def generate_rfis(
    project_id: str,
    db: DB,
    _: CurrentUser,
    run_id: str | None = Query(default=None),
):
    """Synchronously draft an RFI list. Returns the structured list.

    One Opus call (~30s, ~$0.50). Caches under
    <storage_root>/exports/<project_id>/rfi_<run_id>.json so subsequent
    GETs serve from cache. To regenerate, call this endpoint again —
    the cache is overwritten.
    """
    proj = await _ensure_project(db, project_id)
    rid = await _resolve_run_id(db, project_id, run_id)

    result = await rfi_generator.generate_for_run(rid)
    cache_path = _rfi_cache_path(project_id, rid)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cached = {
        "project_id": project_id,
        "project_name": proj.name,
        "run_id": rid,
        "rfi_count": result.rfi_count,
        "cost_usd": result.cost_usd,
        "rfi_list": result.rfi_list,
    }
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(cached, f, indent=2, ensure_ascii=False)
    return cached


@router.get("")
async def get_rfis(
    project_id: str,
    db: DB,
    _: CurrentUser,
    run_id: str | None = Query(default=None),
):
    proj = await _ensure_project(db, project_id)
    rid = await _resolve_run_id(db, project_id, run_id)
    cache_path = _rfi_cache_path(project_id, rid)
    if not cache_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "no RFI list cached for this run — POST /generate first"
            ),
        )
    with cache_path.open("r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/markdown", response_class=PlainTextResponse)
async def get_rfis_markdown(
    project_id: str,
    db: DB,
    _: CurrentUser,
    run_id: str | None = Query(default=None),
):
    """Markdown render of the cached RFI list."""
    proj = await _ensure_project(db, project_id)
    rid = await _resolve_run_id(db, project_id, run_id)
    cache_path = _rfi_cache_path(project_id, rid)
    if not cache_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no RFI list cached for this run — POST /generate first",
        )
    with cache_path.open("r", encoding="utf-8") as f:
        cached = json.load(f)
    result = rfi_generator.RFIGenerationResult(
        rfi_count=cached.get("rfi_count", 0),
        cost_usd=cached.get("cost_usd", 0.0),
        rfi_list=cached.get("rfi_list") or [],
    )
    return rfi_generator.render_markdown(result, project_name=proj.name)
