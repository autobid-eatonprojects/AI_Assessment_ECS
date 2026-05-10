"""Turns Phase-2 structured extraction (and Phase-1 page text for non-drawings)
into atomic search chunks.

A chunk is what the retriever returns — one searchable item with a single
citation back to a page + bbox. The granularity choices here directly drive
search quality:

- One chunk per schedule (the whole table as text — rows aren't split because
  they're meaningless without the column headers).
- One chunk per note (paragraphs are already the right size).
- One chunk per cross-reference.
- One chunk per entity (with sheet context so "F6.0" alone isn't ambiguous).
- One per-page summary chunk (sheet metadata — gives the retriever a coarse
  landing point for queries about the page as a whole).
- For non-drawing docs, one chunk per page using PyMuPDF text extraction,
  split into ~1500-token windows if pages are long.

Each chunk text is short (< 2k tokens) and self-contained. The contextualizer
later prepends 1–2 lines of project / sheet context so the embedding sits in
a meaningful neighbourhood.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import fitz  # PyMuPDF
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Chunk,
    Document,
    DocumentPage,
    ExtractedCrossReference,
    ExtractedEntity,
    ExtractedNote,
    ExtractedSchedule,
    PageExtraction,
)
from ..services.storage import storage

log = logging.getLogger(__name__)


# Aim for chunks under this many characters when splitting page text.
# (~1500 tokens at 4 chars/token average.)
_PAGE_TEXT_CHUNK_CHARS = 6000
_PAGE_TEXT_OVERLAP_CHARS = 400


@dataclass
class ChunkPayload:
    chunk_type: str
    text: str
    source_id: str | None = None
    page_id: str | None = None
    page_number: int | None = None
    extra: dict | None = None
    bbox: dict | None = None
    # CSI section this chunk belongs to (e.g. "08 14 16"). Populated by
    # _chunks_from_other for written-spec docs as it walks pages in order
    # and tracks the active SECTION header. Null for non-spec chunks.
    csi_section: str | None = None


# SECTION header pattern — "SECTION 08 14 16" or just "08 14 16" at the
# start of a line.
_SECTION_HEADER_RE = re.compile(
    r"(?:^|\n)\s*(?:SECTION\s+)?(\d{2}\s+\d{2}\s+\d{2})\b",
    re.IGNORECASE | re.MULTILINE,
)


def _format_schedule(s: ExtractedSchedule) -> str:
    lines = [f"Schedule: {s.name}", "Columns: " + " | ".join(s.columns)]
    for i, row in enumerate(s.rows, 1):
        cells = [f"{c}={row.get(c, '')}" for c in s.columns if row.get(c) not in (None, "")]
        lines.append(f"Row {i}: " + "; ".join(cells))
    return "\n".join(lines)


def _format_entity(e: ExtractedEntity) -> str:
    base = f"[{e.entity_type}] {e.value}"
    if e.extra:
        base += f" ({json.dumps(e.extra)})"
    return base


def _format_cross_reference(cr: ExtractedCrossReference) -> str:
    target = cr.target_sheet
    if cr.detail_id:
        target += f" / detail {cr.detail_id}"
    if cr.context:
        return f"Cross-reference to {target}: {cr.context}"
    return f"Cross-reference to {target}"


def _split_page_text(text: str) -> list[str]:
    """Window long page text into overlapping chunks. Short text returns as one."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= _PAGE_TEXT_CHUNK_CHARS:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + _PAGE_TEXT_CHUNK_CHARS)
        out.append(text[start:end])
        if end == len(text):
            break
        start = end - _PAGE_TEXT_OVERLAP_CHARS
    return out


async def _chunks_from_drawing(db: AsyncSession, doc: Document) -> list[ChunkPayload]:
    """Build chunks from Phase-2 extraction rows."""
    payloads: list[ChunkPayload] = []

    pe_result = await db.execute(
        select(PageExtraction)
        .where(PageExtraction.document_id == doc.id)
        .where(PageExtraction.status == "ready")
        .order_by(PageExtraction.page_number)
    )
    page_extractions = list(pe_result.scalars().all())

    for pe in page_extractions:
        meta_extra = {
            "sheet_number": pe.sheet_number,
            "sheet_title": pe.sheet_title,
            "discipline": pe.discipline,
            "drawing_scale": pe.drawing_scale,
        }

        # Page-level summary chunk
        summary_parts = [
            f"Sheet {pe.sheet_number or ''} {pe.sheet_title or ''}".strip(),
        ]
        if pe.discipline:
            summary_parts.append(f"Discipline: {pe.discipline}")
        if pe.drawing_scale:
            summary_parts.append(f"Scale: {pe.drawing_scale}")
        summary_text = " — ".join(p for p in summary_parts if p)
        if summary_text:
            payloads.append(
                ChunkPayload(
                    chunk_type="page_summary",
                    text=summary_text,
                    source_id=pe.id,
                    page_id=pe.page_id,
                    page_number=pe.page_number,
                    extra=meta_extra,
                )
            )

        # Schedules
        s_result = await db.execute(
            select(ExtractedSchedule).where(ExtractedSchedule.page_extraction_id == pe.id)
        )
        for s in s_result.scalars().all():
            payloads.append(
                ChunkPayload(
                    chunk_type="schedule",
                    text=_format_schedule(s),
                    source_id=s.id,
                    page_id=pe.page_id,
                    page_number=pe.page_number,
                    extra={**meta_extra, "schedule_name": s.name},
                    bbox=s.bbox,
                )
            )

        # Notes
        n_result = await db.execute(
            select(ExtractedNote).where(ExtractedNote.page_extraction_id == pe.id)
        )
        for n in n_result.scalars().all():
            payloads.append(
                ChunkPayload(
                    chunk_type="note",
                    text=n.text,
                    source_id=n.id,
                    page_id=pe.page_id,
                    page_number=pe.page_number,
                    extra=meta_extra,
                    bbox=n.bbox,
                )
            )

        # Cross-references
        cr_result = await db.execute(
            select(ExtractedCrossReference).where(
                ExtractedCrossReference.page_extraction_id == pe.id
            )
        )
        for cr in cr_result.scalars().all():
            payloads.append(
                ChunkPayload(
                    chunk_type="cross_reference",
                    text=_format_cross_reference(cr),
                    source_id=cr.id,
                    page_id=pe.page_id,
                    page_number=pe.page_number,
                    extra={**meta_extra, "target_sheet": cr.target_sheet},
                    bbox=cr.bbox,
                )
            )

        # Entities
        e_result = await db.execute(
            select(ExtractedEntity).where(ExtractedEntity.page_extraction_id == pe.id)
        )
        for ent in e_result.scalars().all():
            payloads.append(
                ChunkPayload(
                    chunk_type="entity",
                    text=_format_entity(ent),
                    source_id=ent.id,
                    page_id=pe.page_id,
                    page_number=pe.page_number,
                    extra={**meta_extra, "entity_type": ent.entity_type},
                    bbox=ent.bbox,
                )
            )

    return payloads


async def _chunks_from_other(db: AsyncSession, doc: Document) -> list[ChunkPayload]:
    """Chunk non-drawing docs from `DocumentPage.text_content`.

    `text_content` is populated upstream by either:
      - the renderer (native PyMuPDF text for digital PDFs), or
      - the OCR service (Gemini Flash for scanned PDFs).
    Either way the chunker treats it the same.

    For written-spec docs we additionally track the active CSI SECTION
    header as we walk pages in order, and stamp each emitted chunk with
    `csi_section`. Lets section_extractor pull all chunks for one section
    with a single indexed equality query.
    """
    payloads: list[ChunkPayload] = []
    page_result = await db.execute(
        select(DocumentPage)
        .where(DocumentPage.document_id == doc.id)
        .order_by(DocumentPage.page_number)
    )
    pages = list(page_result.scalars().all())
    if not pages:
        return payloads

    is_spec = doc.doc_type == "written-spec"
    current_section: str | None = None

    for p in pages:
        text = (p.text_content or "").strip()
        # Update active section before emitting this page's chunks. The
        # section header may appear partway through the page; chunks
        # emitted from that page still belong to the new section since
        # spec sections always start at the top of a fresh page.
        if is_spec and text:
            for m in _SECTION_HEADER_RE.finditer(text):
                current_section = m.group(1).strip()

        if not text:
            payloads.append(
                ChunkPayload(
                    chunk_type="page_summary",
                    text=f"{doc.filename} — page {p.page_number} (no extractable text)",
                    page_id=p.id,
                    page_number=p.page_number,
                    extra={"doc_type": doc.doc_type, "source": doc.source},
                    csi_section=current_section if is_spec else None,
                )
            )
            continue

        pieces = _split_page_text(text)
        for i, piece in enumerate(pieces):
            payloads.append(
                ChunkPayload(
                    chunk_type="page_text",
                    text=piece,
                    page_id=p.id,
                    page_number=p.page_number,
                    extra={
                        "doc_type": doc.doc_type,
                        "source": doc.source,
                        "text_source": p.text_source,
                        "chunk_index": i,
                        "total_chunks": len(pieces),
                    },
                    csi_section=current_section if is_spec else None,
                )
            )

    return payloads


async def chunks_for_document(db: AsyncSession, doc: Document) -> list[ChunkPayload]:
    """Dispatch to the right chunker based on doc_type."""
    if doc.doc_type == "drawing-set":
        return await _chunks_from_drawing(db, doc)
    return await _chunks_from_other(db, doc)


def contextualize(payload: ChunkPayload, document_filename: str) -> str:
    """Templated 1–2 line context prepended to each chunk before embedding.

    Project-agnostic: pulls only sheet metadata + doc filename from the chunk
    itself. We can swap this for a Claude Haiku call (true Anthropic
    Contextual Retrieval) when retrieval quality plateaus.
    """
    extra = payload.extra or {}
    sheet = extra.get("sheet_number")
    title = extra.get("sheet_title")
    discipline = extra.get("discipline")

    parts: list[str] = [f"Document: {document_filename}"]
    if payload.page_number is not None:
        parts.append(f"page {payload.page_number}")
    if sheet:
        parts.append(f"sheet {sheet}")
    if title and title != sheet:
        parts.append(f"({title})")
    if discipline:
        parts.append(f"[{discipline}]")
    if payload.chunk_type != "page_summary":
        parts.append(f"-> {payload.chunk_type}")

    header = " ".join(parts)
    return f"{header}\n{payload.text}"


async def write_chunks(
    db: AsyncSession,
    project_id: str,
    document_id: str,
    payloads: list[ChunkPayload],
    document_filename: str,
) -> list[Chunk]:
    """Idempotently replace all chunks for a document."""
    # Wipe existing chunks for this doc (idempotent re-indexing)
    existing = await db.execute(select(Chunk).where(Chunk.document_id == document_id))
    for old in existing.scalars().all():
        await db.delete(old)
    await db.flush()

    rows: list[Chunk] = []
    for p in payloads:
        c = Chunk(
            project_id=project_id,
            document_id=document_id,
            page_id=p.page_id,
            page_number=p.page_number,
            chunk_type=p.chunk_type,
            source_id=p.source_id,
            text=p.text,
            contextualized_text=contextualize(p, document_filename),
            extra=p.extra,
            bbox=p.bbox,
            csi_section=p.csi_section,
            embedded=False,
        )
        db.add(c)
        rows.append(c)
    await db.commit()
    log.info(
        "chunker: wrote %d chunks for %s (%s)",
        len(rows),
        document_filename,
        document_id,
    )
    return rows
