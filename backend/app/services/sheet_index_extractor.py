"""P2 — Sheet Index extractor.

Reads the cover sheet (CVR / G-001 / first sheet of a drawing-set) and
returns the authoritative master list of sheets in the set: sheet number,
title, discipline, position in set, optional revision marker.

Why this is separate from per-page vision extraction:
  - Per-page vision_extractor returns one PageExtraction per page, with
    a best-effort sheet_number. Misses happen on cluttered sheets.
  - Sheet Index is the master truth: every sheet that exists, even if
    per-page extraction missed it.
  - Cross-reference validation (W8): when a drawing says "see S2.1", we
    confirm S2.1 is in the index. If not, flag as broken xref.
  - Discipline tagging for P3: 9 discipline agents each filter to their
    discipline's sheets via SheetIndex.discipline.

Cost: 1 Sonnet 4.6 vision call per drawing-set document. ~$0.10 / project.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Document, DocumentPage, SheetIndex
from .storage import storage
from .llm_log import record_call, usage_from_anthropic
from .renderer import render_page_at_dpi

log = logging.getLogger(__name__)


_EXTRACT_TOOL = {
    "name": "extract_sheet_index",
    "description": (
        "Extract every sheet listed in the project's drawing-set cover "
        "sheet (CVR / G-001 / sheet index page). Each sheet has a sheet_id "
        "(e.g. 'A1.1', 'S2.3', 'M0.1', 'FP-101'), a title, and a "
        "discipline. Discipline is derived from the prefix character per "
        "AEC convention: A=architectural, S=structural, M=mechanical, "
        "E=electrical, P=plumbing, FP=fire-protection, C=civil, "
        "I=interior, L=landscape, G=general. If the sheet is for a "
        "discipline not matching its prefix (rare retrofit case), record "
        "what the index actually says."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sheets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sheet_id": {
                            "type": "string",
                            "description": "Sheet number as printed (e.g. 'A1.1', 'S2.3')",
                        },
                        "title": {
                            "type": "string",
                            "description": "Sheet title as listed in the index",
                        },
                        "discipline": {
                            "type": "string",
                            "enum": [
                                "architectural", "structural", "mechanical",
                                "electrical", "plumbing", "fire-protection",
                                "civil", "site", "interior", "landscape",
                                "general", "other",
                            ],
                        },
                        "set_order": {
                            "type": "integer",
                            "description": "Position in the listed order, 1-indexed",
                        },
                        "revision": {
                            "type": ["string", "null"],
                            "description": "Revision marker if printed (e.g. 'Rev 2')",
                        },
                    },
                    "required": ["sheet_id", "title", "discipline", "set_order"],
                },
            },
            "cover_sheet_id": {
                "type": ["string", "null"],
                "description": (
                    "The cover sheet's own sheet_id (e.g. 'CVR', 'G-001'). "
                    "Used for audit; null if the cover sheet doesn't list "
                    "itself."
                ),
            },
        },
        "required": ["sheets"],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT = """\
You are reading the COVER SHEET of a construction drawing set. Find the \
"SHEET INDEX" (or "DRAWING INDEX" or "DRAWING LIST") section and extract \
EVERY sheet listed.

The index is usually a table or column list with rows like:
    A1.1   ARCHITECTURAL FLOOR PLAN
    A1.2   ARCHITECTURAL ROOF PLAN
    S1.1   FOUNDATION PLAN
    M0.1   MECHANICAL SCHEDULES & NOTES
    ...

Some indexes group by discipline (ARCHITECTURAL header, then A1.1 / A1.2 / \
A2.1; STRUCTURAL header, then S1.1 / S2.1; etc.). Capture every sheet \
regardless of grouping.

Map each sheet's discipline from its prefix:
    A   → architectural        S   → structural
    M   → mechanical           E   → electrical
    P   → plumbing             FP  → fire-protection
    C   → civil                I   → interior
    L   → landscape            G   → general
Anything else → other.

If the sheet's printed discipline contradicts its prefix (rare), trust \
what the index actually says.

Use extract_sheet_index now. Be exhaustive — missing sheets cause \
downstream cross-reference validation failures.
"""


@dataclass
class _ExtractResult:
    sheets: list[dict]
    cover_sheet_id: str | None
    cost_usd: float
    latency_ms: int


_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def _call_sonnet(image_path: Path, project_id: str, document_id: str) -> _ExtractResult:
    """One Sonnet vision call against the cover sheet."""
    import base64

    client = _get_client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    raw = image_path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    t0 = time.perf_counter()
    msg = await client.messages.create(
        model=settings.vision_model,
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_sheet_index"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract the full sheet index from this cover sheet."
                        ),
                    },
                ],
            }
        ],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "extract_sheet_index"
        ):
            payload = block.input
            break

    cost: float = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="sheet-index",
            model=settings.vision_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
            document_id=document_id,
        )
        cost = c or 0.0
        await db.commit()

    return _ExtractResult(
        sheets=list(payload.get("sheets") or []),
        cover_sheet_id=payload.get("cover_sheet_id"),
        cost_usd=cost,
        latency_ms=latency_ms,
    )


async def extract_for_document(
    document_id: str, *, dpi: int = 300
) -> tuple[int, float]:
    """Extract the sheet index for one drawing-set document.

    Idempotent — wipes existing SheetIndex rows for this document before
    inserting fresh ones, so re-running on a re-rendered cover sheet
    replaces the old manifest cleanly.

    Returns (sheets_count, cost_usd). 0 sheets means we couldn't find
    an index on the cover sheet (might be a single-page sheet with no
    formal index), which is information in itself.
    """
    async with SessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            raise ValueError(f"document {document_id} not found")
        if doc.doc_type != "drawing-set":
            log.info(
                "sheet_index: skipping %s — not a drawing-set", document_id
            )
            return 0, 0.0
        first_page = (
            await db.execute(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_number)
                .limit(1)
            )
        ).scalar_one_or_none()
        if first_page is None:
            log.warning("sheet_index: no pages for %s", document_id)
            return 0, 0.0
        project_id = doc.project_id
        # Document.storage_path is project-relative; convert to absolute
        source_path = storage.absolute_path(doc.storage_path)
        page_number = first_page.page_number

    # Re-render at higher DPI for max OCR/vision quality on the cover sheet
    image_path = await render_page_at_dpi(
        source_path, document_id, page_number, dpi=dpi
    )

    result = await _call_sonnet(image_path, project_id, document_id)
    log.info(
        "sheet_index: extracted %d sheets from %s in %dms ($%.4f)",
        len(result.sheets),
        document_id,
        result.latency_ms,
        result.cost_usd,
    )

    # Persist (idempotent: wipe + insert)
    async with SessionLocal() as db:
        await db.execute(
            SheetIndex.__table__.delete().where(
                SheetIndex.document_id == document_id
            )
        )
        for entry in result.sheets:
            sheet_id = (entry.get("sheet_id") or "").strip()
            if not sheet_id:
                continue
            db.add(
                SheetIndex(
                    project_id=project_id,
                    document_id=document_id,
                    sheet_id=sheet_id,
                    title=(entry.get("title") or "").strip() or None,
                    discipline=entry.get("discipline"),
                    set_order=entry.get("set_order"),
                    revision=entry.get("revision"),
                )
            )
        await db.commit()

    return len(result.sheets), result.cost_usd


async def extract_for_project(project_id: str) -> dict:
    """Run the sheet-index extractor against every drawing-set doc in
    a project. Useful to backfill after the model is added.
    """
    async with SessionLocal() as db:
        docs = (
            await db.execute(
                select(Document)
                .where(Document.project_id == project_id)
                .where(Document.doc_type == "drawing-set")
            )
        ).scalars().all()

    total_sheets = 0
    total_cost = 0.0
    for doc in docs:
        try:
            sheets, cost = await extract_for_document(doc.id)
            total_sheets += sheets
            total_cost += cost
        except Exception as e:  # noqa: BLE001
            log.exception(
                "sheet_index: failed on %s: %s", doc.filename, e
            )

    return {
        "project_id": project_id,
        "documents": len(docs),
        "sheets_extracted": total_sheets,
        "cost_usd": total_cost,
    }


# -----------------------------------------------------------------------------
# Read helpers consumed by P3 discipline agents and the cross-reference
# validator (W8 mitigation).
# -----------------------------------------------------------------------------


async def get_sheet_manifest(project_id: str) -> list[SheetIndex]:
    """Return every SheetIndex row for a project, ordered by set_order."""
    async with SessionLocal() as db:
        return (
            await db.execute(
                select(SheetIndex)
                .where(SheetIndex.project_id == project_id)
                .order_by(SheetIndex.set_order)
            )
        ).scalars().all()


async def known_sheet_ids(project_id: str) -> set[str]:
    """Set of authoritative sheet IDs for cross-reference validation."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(SheetIndex.sheet_id).where(
                    SheetIndex.project_id == project_id
                )
            )
        ).all()
    return {r[0] for r in rows if r[0]}


async def discipline_for_sheet(project_id: str, sheet_id: str) -> str | None:
    """Look up a sheet's discipline tag from the master index."""
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(SheetIndex.discipline)
                .where(SheetIndex.project_id == project_id)
                .where(SheetIndex.sheet_id == sheet_id)
            )
        ).first()
    return row[0] if row else None
