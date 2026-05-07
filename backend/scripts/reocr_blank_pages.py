"""Re-run OCR on DocumentPages whose text_content came back empty.

Why this exists: the OCR pipeline added retry-on-empty + scan-preprocessing
fallbacks AFTER some specs were already processed. Pages that the original
pass left blank now succeed when re-run with current code. Spot-check on
the Elks spec book showed 117/370 spec pages blank, 14 CSI sections >50%
blank — and re-running OCR on a sample blank page returned 1742 chars of
clean text on the first try.

This script:
  1. Finds DocumentPages with empty text_content for one project's
     written-spec docs.
  2. Re-runs `ocr.ocr_image` on each (concurrent, semaphore-capped).
  3. Updates `DocumentPage.text_content` + `text_source='ocr-rerun'`.
  4. Calls `indexer.index_document` for each affected spec doc, which
     wipes and rebuilds chunks + embeddings + BM25 index from the new
     text.

Important: re-indexing wipes existing Chunk rows and inserts fresh ones
with new UUIDs. Any ScopeCitation pointing to the old chunk IDs will
become dangling. That's acceptable here because the existing scope run
was already evaluated against the (incomplete) old chunks; the next
scope run will pick up the now-complete chunks. After this script
finishes, kick off a new scope run from the UI to capture the gains.

Usage:
  cd backend
  PYTHONPATH=. python scripts/reocr_blank_pages.py <project_id> [--all]

  --all : re-OCR every spec page, not just the blank ones. Useful after
          an OCR-pipeline change (e.g. fixing the ensemble picker so
          Mistral results are no longer silently dropped) where pages
          that already have text could now get a better transcription
          from a different provider.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from sqlalchemy import func as sql_func, or_, select
from sqlalchemy.sql import update as sql_update

from app.database import SessionLocal
from app.models import Document, DocumentPage
from app.services import indexer
from app.services.llm_log import record_call
from app.services.ocr import OCRUnavailable, ocr_image
from app.services.storage import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("reocr")


# Same dispatch sem the processor uses — keeps load on the OCR provider sane
_DISPATCH_SEM = asyncio.Semaphore(4)


async def _reocr_one(
    page_id: str,
    page_number: int,
    image_rel: str,
    project_id: str,
    document_id: str,
) -> tuple[int, bool]:
    """Re-OCR one page. Returns (chars_recovered, succeeded)."""
    image_path = storage.absolute_path(image_rel)
    async with _DISPATCH_SEM:
        try:
            r = await asyncio.wait_for(ocr_image(image_path), timeout=90.0)
        except asyncio.TimeoutError:
            log.warning("page %d timed out — skipping", page_number)
            return 0, False
        except OCRUnavailable as e:
            log.warning("page %d OCR unavailable: %s", page_number, e)
            return 0, False
        except Exception as e:  # noqa: BLE001
            log.warning("page %d OCR error: %s", page_number, e)
            return 0, False

    text = (r.text or "").strip()
    if not text:
        log.info("page %d still empty after re-OCR", page_number)
        return 0, False

    # `text_source` column is varchar(16). "ocr-rerun-mistral" is 17 chars
    # (one over). Drop the rerun- marker in text_source — the rerun signal
    # already lives in LLMCall.purpose='ocr-rerun' for audit. Use the same
    # ocr-{provider} convention as the original processor pass.
    async with SessionLocal() as db:
        await db.execute(
            sql_update(DocumentPage)
            .where(DocumentPage.id == page_id)
            .values(text_content=r.text, text_source=f"ocr-{r.provider}")
        )
        await record_call(
            db,
            purpose="ocr-rerun",
            model=("gemini-2.5-flash" if r.provider == "gemini" else "mistral-ocr-latest"),
            usage=r.usage,
            latency_ms=r.latency_ms,
            project_id=project_id,
            document_id=document_id,
            provider=r.provider,
        )
        await db.commit()

    log.info("page %d recovered %d chars (%s)", page_number, len(text), r.provider)
    return len(text), True


async def reocr_project(project_id: str, *, all_pages: bool = False) -> None:
    # 1. Find spec docs + target pages
    async with SessionLocal() as db:
        spec_docs = list((await db.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.doc_type == "written-spec",
            )
        )).scalars().all())
        if not spec_docs:
            log.info("no written-spec docs for project %s — nothing to do", project_id)
            return

        all_blanks: list[tuple[str, str, int, str]] = []  # (page_id, doc_id, page_num, image_rel)
        for d in spec_docs:
            q = select(DocumentPage).where(DocumentPage.document_id == d.id)
            if not all_pages:
                q = q.where(or_(
                    DocumentPage.text_content.is_(None),
                    sql_func.length(DocumentPage.text_content) == 0,
                ))
            rows = list((await db.execute(q.order_by(DocumentPage.page_number))).scalars().all())
            for p in rows:
                all_blanks.append((p.id, d.id, p.page_number, p.image_path))
            label = "pages" if all_pages else "blank pages"
            log.info("doc %s (%s): %d %s", d.filename, d.id[:8], len(rows), label)

    if not all_blanks:
        log.info("nothing to OCR — done")
        return

    label = "all" if all_pages else "blank"
    log.info("re-OCRing %d %s pages...", len(all_blanks), label)
    t0 = time.perf_counter()

    results = await asyncio.gather(*(
        _reocr_one(pid, image_rel=ipath, page_number=pnum,
                   document_id=did, project_id=project_id)
        for pid, did, pnum, ipath in all_blanks
    ))
    chars_recovered = sum(c for c, _ in results)
    pages_recovered = sum(1 for _, ok in results if ok)
    elapsed = time.perf_counter() - t0
    log.info(
        "re-OCR complete: %d/%d pages recovered, %d chars recovered, %.1fs",
        pages_recovered, len(all_blanks), chars_recovered, elapsed,
    )

    # 2. Re-index every spec doc that had recovered pages — wipes chunks +
    # rebuilds embeddings + BM25 from the now-complete text.
    affected_doc_ids = {did for (_, did, _, _), (_, ok) in zip(all_blanks, results) if ok}
    if not affected_doc_ids:
        log.info("no pages recovered text — skipping re-index")
        return

    log.info("re-indexing %d spec doc(s) with recovered text...", len(affected_doc_ids))
    for doc_id in affected_doc_ids:
        out = await indexer.index_document(doc_id)
        log.info(
            "indexed %s: chunks=%s embedded=%s cost=$%.4f",
            doc_id[:8],
            out.get("chunks"),
            out.get("embedded"),
            out.get("cost_usd", 0.0),
        )

    log.info("done. Kick off a new scope run from the UI to capture the recovered content.")


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(
            "Usage: python scripts/reocr_blank_pages.py <project_id> [--all]",
            file=sys.stderr,
        )
        sys.exit(2)
    await reocr_project(args[0], all_pages=("--all" in flags))


if __name__ == "__main__":
    asyncio.run(main())
