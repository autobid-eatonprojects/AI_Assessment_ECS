"""Phase 2 extraction endpoints.

Routes:
    GET    /api/projects/{p}/documents/{d}/extraction
        → DocumentExtractionOverview: per-page status summary + totals.
    GET    /api/projects/{p}/documents/{d}/pages/{n}/extraction
        → Full PageExtractionOut: schedules + notes + refs + entities.
    POST   /api/projects/{p}/documents/{d}/pages/{n}/reextract
        → Re-runs vision pre-pass on one page (failure isolation).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..models import (
    Document,
    DocumentPage,
    ExtractedCrossReference,
    ExtractedEntity,
    ExtractedNote,
    ExtractedSchedule,
    PageExtraction,
    Project,
)
from ..schemas import (
    DocumentExtractionOverview,
    PageExtractionOut,
    PageExtractionSummary,
)
from ..services import processor
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/documents/{document_id}", tags=["extraction"])


async def _ensure_document(db, project_id: str, document_id: str) -> Document:
    result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.project_id == project_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        # also check project exists for clearer errors
        proj = await db.execute(select(Project).where(Project.id == project_id))
        if proj.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
            )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    return doc


@router.get("/extraction", response_model=DocumentExtractionOverview)
async def get_document_extraction(
    project_id: str, document_id: str, db: DB, _: CurrentUser
) -> DocumentExtractionOverview:
    await _ensure_document(db, project_id, document_id)

    pe_result = await db.execute(
        select(PageExtraction)
        .where(PageExtraction.document_id == document_id)
        .order_by(PageExtraction.page_number)
    )
    pes = list(pe_result.scalars().all())

    pe_ids = [pe.id for pe in pes]

    def make_count_query(model, label):
        return select(model.page_extraction_id, func.count().label(label)).where(
            model.page_extraction_id.in_(pe_ids)
        ).group_by(model.page_extraction_id)

    counts: dict[str, dict[str, int]] = {pe.id: {"sched": 0, "notes": 0, "refs": 0, "ents": 0} for pe in pes}
    if pe_ids:
        for model, key in [
            (ExtractedSchedule, "sched"),
            (ExtractedNote, "notes"),
            (ExtractedCrossReference, "refs"),
            (ExtractedEntity, "ents"),
        ]:
            r = await db.execute(make_count_query(model, key))
            for pe_id, n in r.all():
                counts[pe_id][key] = n

    summaries: list[PageExtractionSummary] = []
    total_cost = 0.0
    counts_by_status = {"ready": 0, "failed": 0, "pending": 0, "extracting": 0}
    for pe in pes:
        c = counts.get(pe.id, {"sched": 0, "notes": 0, "refs": 0, "ents": 0})
        summaries.append(
            PageExtractionSummary(
                page_number=pe.page_number,
                status=pe.status,
                sheet_number=pe.sheet_number,
                sheet_title=pe.sheet_title,
                discipline=pe.discipline,
                schedule_count=c["sched"],
                note_count=c["notes"],
                cross_reference_count=c["refs"],
                entity_count=c["ents"],
                cost_usd=pe.cost_usd,
            )
        )
        if pe.cost_usd:
            total_cost += pe.cost_usd
        counts_by_status[pe.status] = counts_by_status.get(pe.status, 0) + 1

    return DocumentExtractionOverview(
        document_id=document_id,
        total_pages=len(pes),
        pages_ready=counts_by_status.get("ready", 0),
        pages_failed=counts_by_status.get("failed", 0),
        pages_pending=counts_by_status.get("pending", 0),
        pages_extracting=counts_by_status.get("extracting", 0),
        total_cost_usd=total_cost,
        pages=summaries,
    )


async def _get_page_extraction(
    db, project_id: str, document_id: str, page_number: int
) -> PageExtraction:
    await _ensure_document(db, project_id, document_id)
    result = await db.execute(
        select(PageExtraction)
        .options(
            selectinload(PageExtraction.schedules),
            selectinload(PageExtraction.notes),
            selectinload(PageExtraction.cross_references),
            selectinload(PageExtraction.entities),
        )
        .join(DocumentPage, DocumentPage.id == PageExtraction.page_id)
        .where(
            PageExtraction.document_id == document_id,
            DocumentPage.page_number == page_number,
        )
    )
    pe = result.scalar_one_or_none()
    if pe is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="page extraction not found"
        )
    return pe


@router.get("/pages/{page_number}/extraction", response_model=PageExtractionOut)
async def get_page_extraction(
    project_id: str,
    document_id: str,
    page_number: int,
    db: DB,
    _: CurrentUser,
) -> PageExtractionOut:
    pe = await _get_page_extraction(db, project_id, document_id, page_number)
    return PageExtractionOut.model_validate(pe)


@router.post("/pages/{page_number}/reextract", response_model=PageExtractionOut)
async def reextract_page(
    project_id: str,
    document_id: str,
    page_number: int,
    db: DB,
    _: CurrentUser,
) -> PageExtractionOut:
    pe = await _get_page_extraction(db, project_id, document_id, page_number)
    pe.status = "pending"
    pe.error = None
    await db.commit()
    await db.refresh(pe)
    processor.schedule_reextract(pe.id)
    return PageExtractionOut.model_validate(pe)
