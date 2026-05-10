"""Stage 7 — Audit log + LLMCall + activity-events endpoints (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import desc, select

from ..models import Document, LLMCall, PageExtraction, Project
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


# Pass C2 — pipeline activity feed.
# Synthesized from existing rows (LLMCall + PageExtraction + Document state
# changes). Returns the most recent N events as a unified time-ordered
# feed so the UI can show "Page 5 OCR'd / Stage flipped to indexing /
# Document ready" alongside user actions.
#
# We intentionally DON'T persist a separate ActivityEvent table — those
# events are derivable from the existing data and persisting them would
# add a write per page-extraction without new information.
@router.get("/activity-events")
async def list_activity_events(
    project_id: str,
    db: DB,
    _: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    """Synthesized pipeline activity feed.

    Pulls recent LLMCall + PageExtraction + Document rows for this project,
    converts each into a typed event, and returns the merged stream sorted
    by time desc. The frontend filter chips (All / Pipeline / Cost / Error)
    operate on the `kind` field client-side.
    """
    await _ensure_project(db, project_id)

    events: list[dict] = []

    # Pipeline events: per-page extraction terminal transitions.
    pe_rows = (
        await db.execute(
            select(
                PageExtraction.id,
                PageExtraction.document_id,
                PageExtraction.page_number,
                PageExtraction.sheet_number,
                PageExtraction.status,
                PageExtraction.extracted_at,
            )
            .join(Document, PageExtraction.document_id == Document.id)
            .where(Document.project_id == project_id)
            .where(PageExtraction.extracted_at.isnot(None))
            .order_by(desc(PageExtraction.extracted_at))
            .limit(limit)
        )
    ).all()
    for pe_id, doc_id, pn, sn, st, ts in pe_rows:
        sheet_label = f"sheet {sn}" if sn else f"page {pn}"
        events.append({
            "kind": "pipeline",
            "ts": ts.isoformat() if ts else None,
            "title": (
                f"Vision pre-pass {st} on {sheet_label}"
                if st == "ready"
                else f"Vision pre-pass FAILED on {sheet_label}"
            ),
            "document_id": doc_id,
            "ref_id": pe_id,
            "severity": "info" if st == "ready" else "error",
        })

    # Cost events: every LLM call.
    llm_rows = (
        await db.execute(
            select(
                LLMCall.id,
                LLMCall.purpose,
                LLMCall.model,
                LLMCall.cost_usd,
                LLMCall.latency_ms,
                LLMCall.status,
                LLMCall.document_id,
                LLMCall.created_at,
            )
            .where(LLMCall.project_id == project_id)
            .order_by(desc(LLMCall.created_at))
            .limit(limit)
        )
    ).all()
    for lc_id, purpose, model, cost, lat, st, doc_id, ts in llm_rows:
        if st != "ok":
            events.append({
                "kind": "error",
                "ts": ts.isoformat() if ts else None,
                "title": f"{purpose or 'API call'} FAILED ({model or 'unknown model'})",
                "document_id": doc_id,
                "ref_id": lc_id,
                "severity": "error",
            })
        else:
            events.append({
                "kind": "cost",
                "ts": ts.isoformat() if ts else None,
                "title": (
                    f"{purpose or 'API call'} · {model or '?'} · "
                    f"${(cost or 0):.4f} · {lat or 0}ms"
                ),
                "document_id": doc_id,
                "ref_id": lc_id,
                "severity": "info",
            })

    # Sort merged stream by ts desc, cap to limit.
    events.sort(key=lambda e: e["ts"] or "", reverse=True)
    return events[:limit]
