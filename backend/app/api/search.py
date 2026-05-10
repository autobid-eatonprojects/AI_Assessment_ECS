"""Search API.

POST /api/projects/{p}/search → SearchResponse
    Hybrid retrieval (dense + BM25) → Cohere Rerank 3 (or Claude fallback) →
    top-K hits with snippets, source page, bounding box, and per-stage scores.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..config import settings
from ..models import Document, Project
from ..schemas import SearchHit, SearchRequest, SearchResponse
from ..services import retriever
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(
    project_id: str, payload: SearchRequest, db: DB, _: CurrentUser
) -> SearchResponse:
    proj = await db.execute(select(Project).where(Project.id == project_id))
    if proj.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )

    # Pre-fetch document filenames so we can label hits
    doc_rows = await db.execute(
        select(Document).where(Document.project_id == project_id)
    )
    doc_names = {d.id: d.filename for d in doc_rows.scalars().all()}

    hits = await retriever.search(
        db,
        project_id=project_id,
        query=payload.query,
        candidates_k=50,
        final_k=payload.top_k,
    )

    rerank_used = "none"
    if hits and hits[0].rerank_score is not None:
        rerank_used = "cohere" if settings.cohere_api_key else "claude"

    out_hits: list[SearchHit] = []
    for h in hits:
        chunk = h.chunk
        meta = chunk.extra or {}
        out_hits.append(
            SearchHit(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_filename=doc_names.get(chunk.document_id, ""),
                page_id=chunk.page_id,
                page_number=chunk.page_number,
                chunk_type=chunk.chunk_type,
                text=chunk.text,
                snippet=h.snippet,
                bbox=chunk.bbox,
                sheet_number=meta.get("sheet_number"),
                sheet_title=meta.get("sheet_title"),
                discipline=meta.get("discipline"),
                dense_score=h.dense_score,
                sparse_score=h.sparse_score,
                rrf_score=h.rrf_score,
                rerank_score=h.rerank_score,
            )
        )

    return SearchResponse(
        query=payload.query,
        hits=out_hits,
        candidates_considered=len(hits),
        rerank_used=rerank_used,
    )
