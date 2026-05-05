"""Background processor for one document.

Pipeline:
    pending -> classifying -> rendering -> [extracting] -> ready

Vision extraction (the `extracting` step) only runs for `drawing-set`
documents. Each page is processed independently so a single bad page
doesn't fail the document. Concurrency is bounded by the semaphore inside
`vision_extractor`.

For Phase 2 we still use fire-and-forget asyncio tasks. Job state lives in
SQLite so we can re-derive what's in flight on restart (see `resume_pending`).
When we add Postgres + a real queue (ARQ / Celery) this orchestrator stays
the same; only the scheduler swaps.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..database import SessionLocal
from ..models import Document, DocumentPage, PageExtraction
from ..services import classifier, gemini_vision_extractor, renderer, vision_extractor
from ..services.llm_log import record_call
from ..services.storage import storage
from ..services.vision_extractor import VisionResult, VisionUnavailable


def _get_vision_extractor():
    """Dispatch to the configured vision provider."""
    if settings.vision_provider == "google":
        return gemini_vision_extractor
    return vision_extractor

log = logging.getLogger(__name__)

# Bounds the number of concurrent Claude vision API calls. Page status flips
# to 'extracting' only after acquiring this slot, so the UI count of "N
# extracting" reflects what's truly in flight (not just queued).
_extract_sem: asyncio.Semaphore | None = None


def _get_extract_semaphore() -> asyncio.Semaphore:
    global _extract_sem
    if _extract_sem is None:
        _extract_sem = asyncio.Semaphore(settings.vision_concurrency)
    return _extract_sem


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------------------
# Document-level status helpers
# -----------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# Vision pre-pass — per page
# -----------------------------------------------------------------------------


async def _seed_page_extractions(document_id: str) -> None:
    """Create one PageExtraction row per DocumentPage in `pending` state."""
    async with SessionLocal() as db:
        # Wipe any prior extractions for idempotency
        result = await db.execute(
            select(PageExtraction).where(PageExtraction.document_id == document_id)
        )
        for old in result.scalars().all():
            await db.delete(old)
        await db.flush()

        result = await db.execute(
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
        )
        for page in result.scalars().all():
            db.add(
                PageExtraction(
                    document_id=document_id,
                    page_id=page.id,
                    page_number=page.page_number,
                    status="pending",
                )
            )
        await db.commit()


async def _persist_extraction(
    page_extraction_id: str,
    document_id: str,
    project_id: str | None,
    result: VisionResult,
) -> None:
    """Persist a successful extraction + LLMCall row in a single transaction."""
    from ..models import (
        ExtractedCrossReference,
        ExtractedEntity,
        ExtractedNote,
        ExtractedSchedule,
    )

    async with SessionLocal() as db:
        # Wipe child rows in case of re-extraction.
        for model in (
            ExtractedSchedule,
            ExtractedNote,
            ExtractedCrossReference,
            ExtractedEntity,
        ):
            old = await db.execute(
                select(model).where(model.page_extraction_id == page_extraction_id)
            )
            for row in old.scalars().all():
                await db.delete(row)
        await db.flush()

        # Update parent
        pe = await db.get(PageExtraction, page_extraction_id)
        if pe is None:
            log.warning("persist_extraction: page_extraction %s not found", page_extraction_id)
            return

        meta = result.extraction.sheet_metadata
        pe.sheet_number = meta.sheet_number
        pe.sheet_title = meta.sheet_title
        pe.discipline = meta.discipline
        pe.drawing_scale = meta.drawing_scale
        pe.status = "ready"
        pe.error = None
        pe.raw_response = result.raw_response
        pe.model = result.model
        pe.latency_ms = result.latency_ms
        pe.extracted_at = _utcnow()

        # Cost log + pe.cost_usd
        cost, _ = await record_call(
            db,
            purpose="vision-extract",
            model=result.model,
            usage=result.usage,
            latency_ms=result.latency_ms,
            project_id=project_id,
            document_id=document_id,
            page_extraction_id=page_extraction_id,
        )
        pe.cost_usd = cost

        # Child rows
        for s in result.extraction.schedules:
            db.add(
                ExtractedSchedule(
                    page_extraction_id=page_extraction_id,
                    name=s.name,
                    columns=s.columns,
                    rows=s.rows,
                    bbox=s.bbox.model_dump() if s.bbox else None,
                )
            )
        for n in result.extraction.notes:
            db.add(
                ExtractedNote(
                    page_extraction_id=page_extraction_id,
                    text=n.text,
                    bbox=n.bbox.model_dump() if n.bbox else None,
                )
            )
        for cr in result.extraction.cross_references:
            db.add(
                ExtractedCrossReference(
                    page_extraction_id=page_extraction_id,
                    target_sheet=cr.target_sheet,
                    detail_id=cr.detail_id,
                    context=cr.context,
                    bbox=cr.bbox.model_dump() if cr.bbox else None,
                )
            )
        for ent in result.extraction.entities:
            db.add(
                ExtractedEntity(
                    page_extraction_id=page_extraction_id,
                    entity_type=ent.entity_type,
                    value=ent.value,
                    bbox=ent.bbox.model_dump() if ent.bbox else None,
                    extra=ent.extra,
                )
            )
        await db.commit()


async def _set_page_extraction_status(
    page_extraction_id: str, status: str, error: str | None = None
) -> None:
    async with SessionLocal() as db:
        pe = await db.get(PageExtraction, page_extraction_id)
        if pe is None:
            return
        pe.status = status
        pe.error = error
        if status in ("ready", "failed"):
            pe.extracted_at = _utcnow()
        await db.commit()


async def _extract_one_page(
    page_extraction_id: str,
    document_id: str,
    project_id: str | None,
    image_path,
    page_number: int,
    document_filename: str,
) -> None:
    """Run vision pre-pass for a single page. Failures are isolated.

    The page stays 'pending' while waiting for the concurrency semaphore so
    the UI shows accurate counts of in-flight vs queued.
    """
    sem = _get_extract_semaphore()
    extractor = _get_vision_extractor()
    async with sem:
        await _set_page_extraction_status(page_extraction_id, "extracting")
        try:
            result = await extractor.extract_page(
                image_path,
                page_number=page_number,
                document_filename=document_filename,
            )
        except VisionUnavailable:
            await _set_page_extraction_status(
                page_extraction_id, "failed", error="ANTHROPIC_API_KEY not set"
            )
            return
        except Exception as e:  # noqa: BLE001
            log.exception(
                "extract: page %d of %s failed", page_number, document_filename
            )
            await _set_page_extraction_status(page_extraction_id, "failed", error=str(e))
            return

    await _persist_extraction(page_extraction_id, document_id, project_id, result)
    log.info(
        "extract: page %d of %s ready (%dms, %d schedules, %d notes, %d refs, %d entities)",
        page_number,
        document_filename,
        result.latency_ms,
        len(result.extraction.schedules),
        len(result.extraction.notes),
        len(result.extraction.cross_references),
        len(result.extraction.entities),
    )


async def _run_vision_prepass(document_id: str) -> tuple[int, int]:
    """Run vision extraction for every page of a document. Returns (ready, failed)."""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Document)
            .options(selectinload(Document.pages))
            .where(Document.id == document_id)
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return 0, 0
        project_id = doc.project_id
        filename = doc.filename
        pages = sorted(doc.pages, key=lambda p: p.page_number)

        result = await db.execute(
            select(PageExtraction).where(PageExtraction.document_id == document_id)
        )
        page_extractions = {pe.page_id: pe for pe in result.scalars().all()}

    tasks: list[asyncio.Task] = []
    for page in pages:
        pe = page_extractions.get(page.id)
        if pe is None:
            continue
        # Skip already-ready pages on resume (only re-run pending/extracting/failed)
        if pe.status == "ready":
            continue
        image_path = storage.absolute_path(page.image_path)
        tasks.append(
            asyncio.create_task(
                _extract_one_page(
                    pe.id,
                    document_id,
                    project_id,
                    image_path,
                    page.page_number,
                    filename,
                )
            )
        )

    await asyncio.gather(*tasks, return_exceptions=True)

    # Recount
    async with SessionLocal() as db:
        result = await db.execute(
            select(PageExtraction).where(PageExtraction.document_id == document_id)
        )
        rows = result.scalars().all()
    ready = sum(1 for r in rows if r.status == "ready")
    failed = sum(1 for r in rows if r.status == "failed")
    return ready, failed


# -----------------------------------------------------------------------------
# Top-level pipeline
# -----------------------------------------------------------------------------


async def process_document(document_id: str) -> None:
    """End-to-end pipeline. Updates document.processing_status as it goes."""
    log.info("processor: starting %s", document_id)

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

    if needs_api_key:
        await _set_status(
            document_id,
            "needs-api-key",
            error="ANTHROPIC_API_KEY not set; pages rendered but unclassified",
        )
        return

    # 3. Vision pre-pass — only for drawing sets.
    async with SessionLocal() as db:
        result = await db.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one_or_none()
        doc_type = doc.doc_type if doc else None

    extraction_error: str | None = None
    if doc_type == "drawing-set" and pages:
        await _seed_page_extractions(document_id)
        await _set_status(document_id, "extracting")
        log.info("processor: vision pre-pass starting for %s (%d pages)", filename, len(pages))
        ready, failed = await _run_vision_prepass(document_id)
        log.info(
            "processor: vision pre-pass done for %s — %d ready, %d failed",
            filename,
            ready,
            failed,
        )
        if failed > 0:
            extraction_error = (
                f"{failed} of {ready + failed} pages failed extraction; re-extract from UI"
            )

    # 4. Phase 3 — index for search.
    await _set_status(document_id, "indexing")
    try:
        from . import indexer

        result = await indexer.index_document(document_id)
        log.info(
            "processor: indexed %s — %d chunks, $%.4f",
            filename,
            result.get("chunks", 0),
            result.get("cost_usd", 0.0),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("processor: indexing failed for %s", filename)
        # Indexing failure shouldn't fail the whole doc — extraction is the
        # primary deliverable. We surface the error but mark the doc ready.
        extraction_error = (
            f"{extraction_error}; indexing: {e}" if extraction_error else f"indexing: {e}"
        )

    await _set_status(document_id, "ready", error=extraction_error)


async def reextract_page(page_extraction_id: str) -> None:
    """Re-run vision on a single page (for the 'Re-extract' button)."""
    async with SessionLocal() as db:
        pe = await db.get(PageExtraction, page_extraction_id)
        if pe is None:
            return
        page = await db.get(DocumentPage, pe.page_id)
        doc = await db.get(Document, pe.document_id)
        if page is None or doc is None:
            return
        image_path = storage.absolute_path(page.image_path)
        page_number = page.page_number
        document_id = doc.id
        project_id = doc.project_id
        filename = doc.filename

    await _extract_one_page(
        page_extraction_id, document_id, project_id, image_path, page_number, filename
    )


# -----------------------------------------------------------------------------
# Resumability — picked up at app startup
# -----------------------------------------------------------------------------


async def resume_pending() -> None:
    """On startup, resume any documents/pages that were mid-flight when we died."""
    async with SessionLocal() as db:
        # Documents stuck mid-pipeline → restart from scratch
        result = await db.execute(
            select(Document).where(
                Document.processing_status.in_(
                    ("pending", "classifying", "rendering", "extracting", "indexing")
                )
            )
        )
        in_flight_docs = list(result.scalars().all())

    for doc in in_flight_docs:
        log.info(
            "resume: re-scheduling %s (was '%s')", doc.filename, doc.processing_status
        )
        schedule(doc.id)


# -----------------------------------------------------------------------------
# Scheduler
# -----------------------------------------------------------------------------

# Keep references so asyncio doesn't garbage-collect mid-flight
_inflight: set[asyncio.Task] = set()


def schedule(document_id: str) -> None:
    task = asyncio.create_task(process_document(document_id))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


def schedule_reextract(page_extraction_id: str) -> None:
    task = asyncio.create_task(reextract_page(page_extraction_id))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
