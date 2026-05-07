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
from ..services import classifier, renderer, vision_extractor
from ..services.llm_log import record_call
from ..services.storage import storage
from ..services.vision_extractor import VisionResult, VisionUnavailable


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


# OCR dispatch limiter. Inside ocr.py the actual provider calls are bounded
# by Semaphore(4) per provider. We wrap each per-page task in *another*
# semaphore here so that asyncio.gather doesn't queue 370 tasks waiting on
# the inner semaphore — without this, the outer wait_for(90s) timer starts
# the moment the task is dispatched and most tasks time out while still
# waiting for the inner semaphore slot, never even reaching the API call.
_ocr_dispatch_sem: asyncio.Semaphore | None = None


def _get_ocr_dispatch_semaphore() -> asyncio.Semaphore:
    global _ocr_dispatch_sem
    if _ocr_dispatch_sem is None:
        # Slightly higher than the inner Sem(4) — gives 4 actively running
        # API calls and 4 ready-to-go (preprocessing/serialization). Larger
        # values just queue tasks under the inner semaphore again.
        _ocr_dispatch_sem = asyncio.Semaphore(8)
    return _ocr_dispatch_sem


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

        # Vendor extraction (Phase 11). Operator-typed vendor_name is the
        # initial value; the classifier reads the actual letterhead and may
        # supply a more accurate / canonical form. Prefer the classifier
        # result when present; record both in vendor_provenance for audit.
        operator_typed = doc.vendor_name
        if c.vendor_name:
            doc.vendor_name = c.vendor_name
        provenance = doc.vendor_provenance or {}
        provenance["operator_typed"] = operator_typed
        provenance["classifier_detected"] = c.vendor_name
        doc.vendor_provenance = provenance

        await db.commit()


async def _persist_pages(document_id: str, pages: list[renderer.RenderedPage]) -> None:
    async with SessionLocal() as db:
        existing = (
            await db.execute(
                select(DocumentPage).where(DocumentPage.document_id == document_id)
            )
        ).scalars().all()

        # Resume guard: if pages already exist with the same count, the
        # renderer already ran successfully on this document — don't wipe
        # OCR text and per-page extractions just to re-insert identical
        # rows. Previously this wipe was unconditional, which caused
        # /resume to lose ~30 minutes of OCR work on every retry.
        # Mismatched count means renderer ran with a different page set
        # (corrupt PDF / mid-render abort), so we still wipe-and-reinsert
        # in that case to keep DocumentPage rows aligned with the source.
        if existing and len(existing) == len(pages):
            log.info(
                "processor: %d pages already persisted for doc %s — "
                "skipping wipe (true resume)",
                len(existing), document_id,
            )
        else:
            if existing:
                log.info(
                    "processor: persisted page count mismatch (had %d, render "
                    "produced %d) — wiping for re-insert",
                    len(existing), len(pages),
                )
            for old in existing:
                await db.delete(old)
            await db.flush()

            for p in pages:
                native = (p.native_text or "").strip()
                db.add(
                    DocumentPage(
                        document_id=document_id,
                        page_number=p.page_number,
                        width=p.width,
                        height=p.height,
                        image_path=str(p.image_path.relative_to(storage.root)),
                        thumbnail_path=str(p.thumbnail_path.relative_to(storage.root)),
                        text_content=native or None,
                        text_source="pymupdf" if native else None,
                    )
                )

        result = await db.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one_or_none()
        if doc is not None:
            doc.page_count = len(pages)
        await db.commit()


async def _ocr_pages_if_needed(document_id: str) -> int:
    """Run Gemini Flash OCR on pages that lack usable native text.

    Picks up pages with NULL text_content AND empty-string text_content.
    Previously this only caught NULL — but empty strings get persisted
    when OCR returns no text on the first pass, which made /resume a
    no-op for previously-failed pages. Empty strings are now treated as
    needing OCR.

    Each OCR call gets a 90-second hard timeout. Mistral occasionally
    accepts a request and never responds, which would otherwise block
    `asyncio.gather()` forever — every other completed task waits on
    the hung one. Timeout returns None for that page, marks the rest
    proceed.
    """
    from sqlalchemy import or_, update as sql_update
    from sqlalchemy.sql import func as sql_func

    from .ocr import OCRUnavailable, ocr_image

    async with SessionLocal() as db:
        result = await db.execute(
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id)
            .where(
                or_(
                    DocumentPage.text_content.is_(None),
                    sql_func.length(DocumentPage.text_content) == 0,
                )
            )
            .order_by(DocumentPage.page_number)
        )
        pages = list(result.scalars().all())
        if not pages:
            return 0
        page_records = [(p.id, p.page_number, p.image_path) for p in pages]
        project_id = (await db.get(Document, document_id)).project_id  # type: ignore[union-attr]

    log.info("ocr: %d pages need OCR for doc %s", len(page_records), document_id)

    dispatch_sem = _get_ocr_dispatch_semaphore()

    async def _one(page_id: str, page_number: int, image_rel: str):
        image_path = storage.absolute_path(image_rel)
        # Acquire the dispatch slot WITHOUT a timeout. Only wrap the actual
        # API call (post-acquisition) in wait_for(90s), so the timer reflects
        # API responsiveness and not queue depth. Without this, dispatching
        # 370 tasks via asyncio.gather makes most of them time out while
        # still queued — the API never gets called.
        async with dispatch_sem:
            try:
                r = await asyncio.wait_for(ocr_image(image_path), timeout=90.0)
            except asyncio.TimeoutError:
                log.warning("ocr: page %d timed out after 90s — skipping", page_number)
                return None
            except OCRUnavailable:
                log.warning("ocr: GOOGLE_API_KEY not set — skipping page %d", page_number)
                return None
            except Exception as e:  # noqa: BLE001
                log.warning("ocr: page %d failed: %s", page_number, e)
                return None
        async with SessionLocal() as db:
            await db.execute(
                sql_update(DocumentPage)
                .where(DocumentPage.id == page_id)
                .values(text_content=r.text, text_source=f"ocr-{r.provider}")
            )
            await record_call(
                db,
                purpose="ocr",
                model="gemini-2.5-flash" if r.provider == "gemini" else "mistral-ocr-latest",
                usage=r.usage,
                latency_ms=r.latency_ms,
                project_id=project_id,
                document_id=document_id,
                provider=r.provider,
            )
            await db.commit()
        return r

    results = await asyncio.gather(
        *(_one(pid, pn, ip) for pid, pn, ip in page_records),
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is not None and not isinstance(r, Exception))
    return ok


# -----------------------------------------------------------------------------
# Vision pre-pass — per page
# -----------------------------------------------------------------------------


async def _seed_page_extractions(document_id: str) -> None:
    """Idempotently ensure one PageExtraction row exists per DocumentPage.

    Insert-or-skip per page. Never wipes existing rows — wiping mid-flight
    rows breaks any in-flight tasks holding their IDs (the result becomes a
    silent 'page_extraction not found' on persist, with the Sonnet API call
    already paid for). Re-processing must go through schedule_reprocess(),
    which cancels in-flight tasks first and clears terminal-state rows
    deliberately, then calls this function on a clean slate.
    """
    async with SessionLocal() as db:
        # What's already there?
        result = await db.execute(
            select(PageExtraction.page_id).where(
                PageExtraction.document_id == document_id
            )
        )
        existing_page_ids = {row[0] for row in result.all()}

        result = await db.execute(
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
        )
        added = 0
        for page in result.scalars().all():
            if page.id in existing_page_ids:
                continue  # already seeded — leave it alone
            db.add(
                PageExtraction(
                    document_id=document_id,
                    page_id=page.id,
                    page_number=page.page_number,
                    status="pending",
                )
            )
            added += 1
        if added:
            await db.commit()
            log.info(
                "seed_page_extractions: doc %s — added %d new rows "
                "(already had %d)",
                document_id, added, len(existing_page_ids),
            )


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
        # Drop discipline-name false positives at write time so they don't
        # later inflate unresolved_cross_reference gap counts. The vision
        # extractor occasionally captures phrases like "Architectural
        # drawings" or "STRUCTURAL" verbatim as `target_sheet`.
        from .discipline_config import is_valid_sheet_id

        for cr in result.extraction.cross_references:
            if not is_valid_sheet_id(cr.target_sheet):
                continue
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
    async with sem:
        await _set_page_extraction_status(page_extraction_id, "extracting")
        try:
            result = await vision_extractor.extract_page(
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
        upload_source = doc.source  # 'project_document' | 'bid_submission'

    # 0. Office-format conversion (Phase 11.5). PyMuPDF + our renderer can
    # only read PDFs and images. If a user uploaded a .doc, .docx, .ppt,
    # .pptx, .odt, .rtf, etc., shell out to LibreOffice headless and
    # convert to PDF first. The original is preserved on disk; the
    # Document.storage_path is repointed to the converted PDF so every
    # downstream stage sees a normal PDF.
    from .office_converter import (
        OfficeConverterUnavailable,
        convert_to_pdf,
        is_office_format,
    )

    if is_office_format(source_path):
        await _set_status(document_id, "converting")
        try:
            pdf_path = await convert_to_pdf(
                source_path, output_dir=source_path.parent / "_converted"
            )
            # Repoint the document at the converted PDF for the rest of the
            # pipeline. Keep filename + size_bytes referencing the upload
            # for UI honesty (the user uploaded a .doc, not a .pdf).
            new_rel = storage.relative_path(pdf_path)
            async with SessionLocal() as db:
                d = await db.get(Document, document_id)
                if d is not None:
                    d.storage_path = new_rel
                    d.content_type = "application/pdf"
                    await db.commit()
            source_path = pdf_path
            content_type = "application/pdf"
            log.info(
                "processor: converted Office %s → PDF for downstream processing",
                filename,
            )
        except OfficeConverterUnavailable as e:
            log.warning("processor: office conversion unavailable: %s", e)
            await _set_status(
                document_id,
                "failed",
                error=(
                    f"office conversion: {e}. Re-upload as PDF or install "
                    "LibreOffice on the server."
                ),
            )
            return
        except Exception as e:  # noqa: BLE001
            log.exception("processor: office conversion failed for %s", filename)
            await _set_status(document_id, "failed", error=f"office conversion: {e}")
            return

    # 1. Classify (taxonomy depends on which side uploaded it)
    needs_api_key = False
    await _set_status(document_id, "classifying")
    try:
        c = await classifier.classify_with_retry(
            source_path, content_type, source=upload_source
        )
        await _persist_classification(document_id, c)
        log.info(
            "processor: classified %s (source=%s) as %s (conf=%.2f)",
            filename,
            upload_source,
            c.doc_type,
            c.confidence,
        )
        # Phase 11: re-canonicalize vendor names on the project after every
        # bid classification. Idempotent + cheap (~$0.001 embed call).
        if upload_source == "bid_submission":
            try:
                from .vendor_canonicalizer import canonicalize_vendors

                async with SessionLocal() as db:
                    doc_row = (
                        await db.execute(
                            select(Document.project_id).where(
                                Document.id == document_id
                            )
                        )
                    ).scalar_one_or_none()
                if doc_row:
                    await canonicalize_vendors(doc_row)
            except Exception as e:  # noqa: BLE001
                log.warning("processor: canonicalize_vendors failed: %s", e)
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

    # 3. Branch by (source, doc_type)
    #
    #    project_document + drawing-set    → Phase 2 vision pre-pass per page
    #    project_document + written-spec   → OCR pages without native text
    #    project_document + trade-list     → no per-page processing
    #    project_document + other          → no per-page processing
    #    bid_submission   + any            → OCR if scanned, otherwise nothing
    #                                        Phase 8 will read the text content
    async with SessionLocal() as db:
        result = await db.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one_or_none()
        doc_type = doc.doc_type if doc else None

    extraction_error: str | None = None

    if upload_source == "project_document" and doc_type == "drawing-set" and pages:
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

        # Post-vision enrichment passes (sheet_index, schedule pipeline,
        # revision blocks, symbol legend). Each pass is independent —
        # they read DocumentPage / PageExtraction and write to their own
        # tables — so the orchestrator runs them concurrently via
        # `asyncio.gather` rather than sequentially. On the Elks drawing
        # set this cuts post-vision wall time from ~6 min to ~3 min
        # (max of the four instead of sum). Per-pass fault isolation
        # is preserved by `run_drawing_enrichment`.
        await _set_status(document_id, "enriching")
        async with SessionLocal() as db:
            doc_row = await db.get(Document, document_id)
            project_id_for_enrichment = doc_row.project_id if doc_row else None
        if project_id_for_enrichment:
            try:
                from .enrichment_passes import (
                    EnrichmentContext,
                    run_drawing_enrichment,
                )

                await run_drawing_enrichment(
                    EnrichmentContext(
                        document_id=document_id,
                        project_id=project_id_for_enrichment,
                        filename=filename,
                    )
                )
            except Exception as e:  # noqa: BLE001 — never break the pipeline
                log.exception("processor: enrichment phase crashed: %s", e)
    elif doc_type in ("written-spec", "bid-quote", "scope-letter") and pages:
        # Text-bearing docs: OCR any page that lacks native text so the
        # downstream chunker has content to index.
        await _set_status(document_id, "ocr")
        try:
            n_ocr = await _ocr_pages_if_needed(document_id)
            log.info("processor: OCR'd %d pages for %s", n_ocr, filename)
        except Exception as e:  # noqa: BLE001
            log.exception("processor: OCR failed for %s", filename)
            extraction_error = f"ocr: {e}"

        # P2 — Spec TOC reconstruction → project-specific CSI subset.
        # Only fires for written-spec docs; reads the project's spec
        # text + the canonical CSI vocab and produces the subset that
        # downstream P3 discipline agents will use to scope their
        # context. Cheap (~$0.05) and idempotent — re-runs as new spec
        # docs are uploaded. Runs after OCR so detected sections are
        # complete.
        if upload_source == "project_document" and doc_type == "written-spec":
            try:
                from .spec_toc_extractor import reconstruct_for_project

                async with SessionLocal() as db:
                    doc_row = await db.get(Document, document_id)
                    project_id_for_toc = doc_row.project_id if doc_row else None
                if project_id_for_toc:
                    toc_result = await reconstruct_for_project(project_id_for_toc)
                    log.info(
                        "processor: P2 spec_toc — %d detected, "
                        "%d confirmed sections ($%.4f) for %s",
                        toc_result["detected_count"],
                        len(toc_result["confirmed"]),
                        toc_result["cost_usd"],
                        filename,
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("processor: P2 spec_toc failed: %s", e)
                # Don't set extraction_error here — spec_toc is best-effort
                # supplementary data, doesn't block the main pipeline.

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

    # Status='ready' guard: for drawing-set docs only mark ready when
    # every per-page extraction reached a terminal state (ready or
    # failed). Without this, the doc shows as ready while the UI
    # still has 5+ pages spinning in 'extracting' — the symptom you'd
    # see is "Why is it ready when not all pages are done?".
    async with SessionLocal() as db:
        from sqlalchemy import func as sql_func

        n_pe_total = (
            await db.execute(
                select(sql_func.count(PageExtraction.id))
                .where(PageExtraction.document_id == document_id)
            )
        ).scalar() or 0
        n_pe_terminal = (
            await db.execute(
                select(sql_func.count(PageExtraction.id))
                .where(PageExtraction.document_id == document_id)
                .where(PageExtraction.status.in_(("ready", "failed")))
            )
        ).scalar() or 0

    if n_pe_total > 0 and n_pe_total != n_pe_terminal:
        # Some per-page extractions still in flight (likely stuck because
        # the asyncio task died mid-flight). Keep the document in
        # 'extracting' so the UI shows the truth and /resume can finish
        # them. The per-page batch above will be re-driven on next resume.
        log.warning(
            "processor: %s — %d/%d page extractions terminal; staying "
            "'extracting' rather than marking ready",
            filename, n_pe_terminal, n_pe_total,
        )
        await _set_status(
            document_id,
            "extracting",
            error=(
                extraction_error
                or f"only {n_pe_terminal}/{n_pe_total} per-page extractions reached terminal state"
            ),
        )
        return

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
    """On startup, resume any documents/pages that were mid-flight when we died.

    Skips documents whose task is already running in-process — schedule()
    is idempotent, but logging the no-op skip is misleading. resume_pending
    is for cold-start; if the process is already alive and tasks are
    registered, those tasks will continue.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Document).where(
                Document.processing_status.in_(
                    ("pending", "classifying", "rendering", "extracting", "indexing", "ocr")
                )
            )
        )
        in_flight_docs = list(result.scalars().all())

    for doc in in_flight_docs:
        if get_inflight_task(doc.id) is not None:
            log.info(
                "resume: %s already has a running task in-process, skipping",
                doc.filename,
            )
            continue
        log.info(
            "resume: re-scheduling %s (status was '%s')",
            doc.filename, doc.processing_status,
        )
        schedule(doc.id)


# -----------------------------------------------------------------------------
# Scheduler
# -----------------------------------------------------------------------------

# Per-document task registry. Lets us:
#   1. Skip duplicate schedule() calls — if a doc is already processing,
#      a second schedule() returns the existing task instead of starting
#      a parallel pipeline that races over the same DB rows.
#   2. Cancel a doc's task when it gets deleted or re-processed.
# Keys are document_ids; values are the asyncio.Task driving process_document.
_inflight_by_doc: dict[str, asyncio.Task] = {}


def get_inflight_task(document_id: str) -> asyncio.Task | None:
    """Return the in-flight task for this document, if one is running."""
    task = _inflight_by_doc.get(document_id)
    if task is not None and task.done():
        return None
    return task


def schedule(document_id: str) -> asyncio.Task:
    """Idempotent schedule. If a task is already running for this doc, return it."""
    existing = _inflight_by_doc.get(document_id)
    if existing is not None and not existing.done():
        log.info(
            "processor: schedule(%s) skipped — task already running",
            document_id,
        )
        return existing

    task = asyncio.create_task(process_document(document_id))
    _inflight_by_doc[document_id] = task

    def _cleanup(t: asyncio.Task) -> None:
        # Only remove if WE are still the registered task. A subsequent
        # cancel + re-schedule may have replaced us.
        if _inflight_by_doc.get(document_id) is t:
            _inflight_by_doc.pop(document_id, None)

    task.add_done_callback(_cleanup)
    return task


async def cancel_document(document_id: str) -> bool:
    """Cancel the in-flight task for this document (if any).

    Returns True if a task was cancelled. Use this before deleting a
    Document or re-seeding its rows — without cancelling, the orphan task
    keeps running, drains the Sonnet/Mistral API quota for results that
    will be discarded, and emits 'page_extraction not found' warnings
    when it tries to persist to deleted rows.
    """
    task = _inflight_by_doc.pop(document_id, None)
    if task is None or task.done():
        return False
    log.info("processor: cancelling in-flight task for doc %s", document_id)
    task.cancel()
    # Don't await — the cancellation propagates through the asyncio loop
    # and the task's own exception handlers run independently. Awaiting
    # here would block the caller (often a synchronous DELETE handler).
    return True


async def schedule_reprocess(document_id: str) -> None:
    """Cancel any in-flight task, clear terminal-state PageExtraction rows
    so seed creates fresh ones, then schedule a new pipeline run.

    This is the only safe way to re-process a document. Just calling
    schedule() while a task is in flight does nothing (idempotent skip).
    """
    await cancel_document(document_id)
    # Clear ONLY terminal-state rows so re-process gets a clean slate
    # without racing in-flight tasks (there shouldn't be any after
    # cancel_document, but be defensive).
    async with SessionLocal() as db:
        result = await db.execute(
            select(PageExtraction)
            .where(PageExtraction.document_id == document_id)
            .where(PageExtraction.status.in_(("ready", "failed", "extracting")))
        )
        for row in result.scalars().all():
            await db.delete(row)
        await db.commit()
    schedule(document_id)


def schedule_reextract(page_extraction_id: str) -> None:
    task = asyncio.create_task(reextract_page(page_extraction_id))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
