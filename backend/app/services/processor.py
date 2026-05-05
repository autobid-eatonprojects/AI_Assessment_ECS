"""Background processor: classify a document, then render its pages.

For Phase 1 we run jobs as fire-and-forget asyncio tasks. Each task opens its
own DB session so it isn't tied to the request that triggered it. When we move
to Phase 2 (heavier vision pre-pass), this swaps to ARQ or Celery — the rest
of the codebase doesn't change.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..database import SessionLocal
from ..models import Document, DocumentPage
from ..services import classifier, renderer
from ..services.storage import storage

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _set_status(document_id: str, status: str, error: str | None = None) -> None:
    async with SessionLocal() as db:
        result = await db.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one_or_none()
        if doc is None:
            return
        doc.processing_status = status
        doc.processing_error = error
        if status in ("ready", "failed", "needs-api-key"):
            doc.processed_at = _utcnow()
        await db.commit()


async def _persist_classification(document_id: str, c: classifier.Classification) -> None:
    async with SessionLocal() as db:
        result = await db.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one_or_none()
        if doc is None:
            return
        doc.doc_type = c.doc_type
        doc.classification_confidence = c.confidence
        doc.classification_reasoning = c.reasoning
        await db.commit()


async def _persist_pages(document_id: str, pages: list[renderer.RenderedPage]) -> None:
    async with SessionLocal() as db:
        # Wipe any prior pages for idempotency
        result = await db.execute(
            select(DocumentPage).where(DocumentPage.document_id == document_id)
        )
        for old in result.scalars().all():
            await db.delete(old)
        await db.flush()

        for p in pages:
            db.add(
                DocumentPage(
                    document_id=document_id,
                    page_number=p.page_number,
                    width=p.width,
                    height=p.height,
                    image_path=str(p.image_path.relative_to(storage.root)),
                    thumbnail_path=str(p.thumbnail_path.relative_to(storage.root)),
                )
            )

        result = await db.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one_or_none()
        if doc is not None:
            doc.page_count = len(pages)
        await db.commit()


async def process_document(document_id: str) -> None:
    """End-to-end: classify -> render. Updates status as it progresses."""
    log.info("processor: starting %s", document_id)

    # Look up the file path
    async with SessionLocal() as db:
        result = await db.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one_or_none()
        if doc is None:
            log.warning("processor: doc %s not found", document_id)
            return
        source_path = storage.absolute_path(doc.storage_path)
        content_type = doc.content_type
        filename = doc.filename

    # 1. Classify
    needs_api_key = False
    await _set_status(document_id, "classifying")
    try:
        c = await classifier.classify_with_retry(source_path, content_type)
        await _persist_classification(document_id, c)
        log.info(
            "processor: classified %s as %s (conf=%.2f)", filename, c.doc_type, c.confidence
        )
    except classifier.ClassifierUnavailable:
        log.warning("processor: no API key — skipping classification for %s", filename)
        needs_api_key = True
        # Continue to render so user can still preview pages.
    except Exception as e:  # noqa: BLE001
        log.exception("processor: classification failed for %s", filename)
        await _set_status(document_id, "failed", error=f"classification: {e}")
        return

    # 2. Render pages
    if not needs_api_key:
        await _set_status(document_id, "rendering")
    try:
        pages = await renderer.render(source_path, document_id)
        await _persist_pages(document_id, pages)
        log.info("processor: rendered %d pages for %s", len(pages), filename)
    except Exception as e:  # noqa: BLE001
        log.exception("processor: rendering failed for %s", filename)
        await _set_status(document_id, "failed", error=f"rendering: {e}")
        return

    final_status = "needs-api-key" if needs_api_key else "ready"
    error = "ANTHROPIC_API_KEY not set; pages rendered but unclassified" if needs_api_key else None
    await _set_status(document_id, final_status, error=error)


# Keep references so asyncio doesn't garbage-collect mid-flight
_inflight: set[asyncio.Task] = set()


def schedule(document_id: str) -> None:
    task = asyncio.create_task(process_document(document_id))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
