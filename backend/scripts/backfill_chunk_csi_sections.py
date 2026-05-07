"""Backfill chunks.csi_section for every existing spec chunk.

Walks each written-spec document page-by-page in order, detects SECTION
headers (regex `SECTION NN NN NN`), and tags every chunk on each page with
the currently-active section. Idempotent — can be re-run after re-indexing.

Run:
  cd backend
  .venv/bin/python -m scripts.backfill_chunk_csi_sections
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import defaultdict

from sqlalchemy import select, update

from app.database import SessionLocal
from app.models import Chunk, Document, DocumentPage


# Spec section header — "SECTION 08 14 16" or just "08 14 16" at the start
# of a line. Allow optional title text afterwards.
_SECTION_HEADER = re.compile(
    r"(?:^|\n)\s*(?:SECTION\s+)?(\d{2}\s+\d{2}\s+\d{2})\b",
    re.IGNORECASE | re.MULTILINE,
)


async def _backfill_doc(db, doc: Document) -> tuple[int, int, int]:
    """For one spec document, build page→section map then update chunks.

    Returns (pages_scanned, sections_detected, chunks_tagged).
    """
    pages = (
        await db.execute(
            select(DocumentPage.page_number, DocumentPage.text_content)
            .where(DocumentPage.document_id == doc.id)
            .order_by(DocumentPage.page_number)
        )
    ).all()
    if not pages:
        return 0, 0, 0

    # Walk pages in order; carry the current section forward.
    page_to_section: dict[int, str | None] = {}
    current: str | None = None
    sections_detected: set[str] = set()
    for page_num, text in pages:
        if text:
            for m in _SECTION_HEADER.finditer(text):
                current = m.group(1).strip()
                sections_detected.add(current)
        page_to_section[page_num] = current

    # Pull all chunks for this doc and group by page so we can issue
    # one UPDATE per (doc, section) batch.
    chunks = (
        await db.execute(
            select(Chunk.id, Chunk.page_number)
            .where(Chunk.document_id == doc.id)
        )
    ).all()
    by_section: dict[str, list[str]] = defaultdict(list)
    untagged = 0
    for chunk_id, page_num in chunks:
        section = page_to_section.get(page_num)
        if section:
            by_section[section].append(chunk_id)
        else:
            untagged += 1

    tagged = 0
    for section, ids in by_section.items():
        await db.execute(
            update(Chunk)
            .where(Chunk.id.in_(ids))
            .values(csi_section=section)
        )
        tagged += len(ids)

    return len(pages), len(sections_detected), tagged


async def main() -> int:
    async with SessionLocal() as db:
        spec_docs = (
            await db.execute(
                select(Document).where(Document.doc_type == "written-spec")
            )
        ).scalars().all()
        if not spec_docs:
            print("no written-spec documents found")
            return 0

        total_pages = 0
        total_sections = 0
        total_tagged = 0
        for doc in spec_docs:
            pages, sections, tagged = await _backfill_doc(db, doc)
            total_pages += pages
            total_sections += sections
            total_tagged += tagged
            print(
                f"  doc={doc.id[:8]}…  filename={doc.filename[:50]}  "
                f"pages={pages}  sections={sections}  chunks_tagged={tagged}"
            )
        await db.commit()

    print(
        f"\nDone. {len(spec_docs)} doc(s), {total_pages} pages, "
        f"{total_sections} sections detected, {total_tagged} chunks tagged."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
