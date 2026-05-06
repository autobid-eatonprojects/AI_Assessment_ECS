"""Stage 7 — Audit log + LLMCall endpoints (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import desc, select

from ..models import LLMCall, Project
from ..schemas import AuditLogOut
from ..services import review_queue
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/audit-log", tags=["audit"])


async def _ensure_project(db, project_id: str) -> Project:
    proj = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return proj


@router.get("", response_model=list[AuditLogOut])
async def list_audit(
    project_id: str,
    db: DB,
    _: CurrentUser,
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditLogOut]:
    await _ensure_project(db, project_id)
    rows = await review_queue.list_audit_log(
        db, project_id, entity_type=entity_type, limit=limit
    )
    return [AuditLogOut.model_validate(r) for r in rows]


@router.get("/llm-calls")
async def list_llm_calls(
    project_id: str,
    db: DB,
    _: CurrentUser,
    purpose: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    """LLMCall rows for the project, paginated by recency.

    Returns dicts (not a Pydantic schema) since LLMCall has many columns and
    the frontend mostly groups/filters.
    """
    await _ensure_project(db, project_id)
    q = (
        select(LLMCall)
        .where(LLMCall.project_id == project_id)
        .order_by(desc(LLMCall.created_at))
        .limit(limit)
    )
    if purpose:
        q = q.where(LLMCall.purpose == purpose)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": r.id,
            "purpose": r.purpose,
            "model": r.model,
            "provider": r.provider,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cost_usd": r.cost_usd,
            "latency_ms": r.latency_ms,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
