"""P2 — revision-block parser (W18 mitigation).

Extracts the revision log from each drawing-sheet's title block. Title
blocks are conventionally on the right side of a drawing sheet (often
bottom-right corner), with a stacked list of revisions:

    Rev | Date     | Description           | By
    0   | 11/26/25 | Issue for Bid          | XYZ
    1   | 12/15/25 | Owner-requested change | XYZ
    2   | 01/10/26 | Add'l plumbing fixtures| ABC

Approach:
  - Crop the right ~25% of the page (title block is almost always there)
  - Sonnet vision with a tight schema returns the revision entries
  - Persist as SheetRevision rows tied back to the page

Per-page cost: ~$0.02-0.04 (cheaper than full-page vision because the
crop is much smaller).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import (
    Document,
    DocumentPage,
    PageExtraction,
    SheetRevision,
)
from .llm_log import record_call, usage_from_anthropic
from .renderer import crop_region, read_image_for_vision
from .storage import storage

log = logging.getLogger(__name__)


# Right-side title block — empirically right 25% of page covers most
# title blocks regardless of sheet size. Tighter crops miss anything
# whose block runs into the drawing area; this is permissive on purpose.
_TITLE_BLOCK_BBOX = (0.75, 0.0, 1.0, 1.0)
_PARSE_DPI = 600
_CONCURRENCY = 4


_PARSE_TOOL = {
    "name": "extract_revisions",
    "description": (
        "Extract the revision log from this title block. The revision log "
        "lists every revision/issue made to this sheet: rev number, "
        "date, brief description, and reviewer initials. Empty list if "
        "no revisions are visible (sheet is at original issue / Rev 0)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sheet_id": {
                "type": "string",
                "description": "Sheet ID as printed on the title block (e.g. 'A1.1', 'S2.3')",
            },
            "revisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rev_number": {
                            "type": "string",
                            "description": "Revision number as printed (usually '0', '1', '2'; sometimes 'A', 'B')",
                        },
                        "rev_date": {
                            "type": ["string", "null"],
                            "description": "Issue date as printed (any format; preserve verbatim)",
                        },
                        "description": {
                            "type": ["string", "null"],
                            "description": "Brief description of the revision",
                        },
                        "by": {
                            "type": ["string", "null"],
                            "description": "Reviewer initials / name as printed",
                        },
                    },
                    "required": ["rev_number"],
                },
            },
        },
        "required": ["revisions"],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT = """\
You are reading the TITLE BLOCK of a construction drawing sheet (right \
side of the sheet). Find the REVISION LOG / REVISION TABLE and extract \
every entry.

The revision log is usually a small table with columns like:
    Rev | Date | Description | By

Or it may be a stacked list. Capture each entry verbatim — don't \
reformat dates, don't expand initials, don't translate descriptions.

Also capture the SHEET ID printed on the title block (e.g. "A1.1" or \
"S2.3") so we can verify it matches what we expected for this page.

If there's no visible revision table, return revisions=[] and that's \
fine — Rev 0 / first-issue sheets don't always print a log.
"""


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


async def _parse_one_page(
    client,
    *,
    project_id: str,
    document_id: str,
    page_extraction: PageExtraction,
    source_path: str,
) -> tuple[list[SheetRevision], float]:
    """Crop the title block + extract revisions."""
    from pathlib import Path

    abs_source = storage.absolute_path(source_path)
    try:
        crop_path = await crop_region(
            abs_source,
            document_id,
            page_extraction.page_number,
            _TITLE_BLOCK_BBOX,
            dpi=_PARSE_DPI,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "revision_block: crop failed for page %d: %s",
            page_extraction.page_number, e,
        )
        return [], 0.0

    raw, media_type = read_image_for_vision(Path(crop_path))
    b64 = base64.standard_b64encode(raw).decode("ascii")

    t0 = time.perf_counter()
    try:
        msg = await client.messages.create(
            model=settings.vision_model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_PARSE_TOOL],
            tool_choice={"type": "tool", "name": "extract_revisions"},
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
                            "text": "Extract the revision log from this title block.",
                        },
                    ],
                }
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "revision_block: API error on page %d: %s",
            page_extraction.page_number, e,
        )
        return [], 0.0
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "extract_revisions"
        ):
            payload = block.input
            break

    cost: float = 0.0
    sheet_id = payload.get("sheet_id") or page_extraction.sheet_number or ""

    rev_rows: list[SheetRevision] = []
    for idx, entry in enumerate(payload.get("revisions") or []):
        if not isinstance(entry, dict):
            continue
        rev_num = entry.get("rev_number")
        if not rev_num:
            continue
        rev_rows.append(
            SheetRevision(
                project_id=project_id,
                document_id=document_id,
                page_extraction_id=page_extraction.id,
                sheet_id=sheet_id,
                rev_number=str(rev_num),
                rev_date=(entry.get("rev_date") or None),
                description=(entry.get("description") or None),
                by=(entry.get("by") or None),
                rev_order=idx + 1,
            )
        )

    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="revision-block",
            model=settings.vision_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
            document_id=document_id,
            page_extraction_id=page_extraction.id,
        )
        cost = c or 0.0
        # Wipe + insert (idempotent on re-run)
        await db.execute(
            SheetRevision.__table__.delete().where(
                SheetRevision.page_extraction_id == page_extraction.id
            )
        )
        for r in rev_rows:
            db.add(r)
        await db.commit()

    return rev_rows, cost


async def parse_for_document(document_id: str) -> dict:
    """Parse the revision block on every page of a drawing-set document."""
    client = _get_client()
    if client is None:
        return {"document_id": document_id, "pages": 0, "revisions": 0, "cost_usd": 0.0}

    async with SessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None or doc.doc_type != "drawing-set":
            return {"document_id": document_id, "pages": 0, "revisions": 0, "cost_usd": 0.0}
        pe_rows = (
            await db.execute(
                select(PageExtraction)
                .where(PageExtraction.document_id == document_id)
                .order_by(PageExtraction.page_number)
            )
        ).scalars().all()
        project_id = doc.project_id
        source_path_str = doc.storage_path

    if not pe_rows:
        return {"document_id": document_id, "pages": 0, "revisions": 0, "cost_usd": 0.0}

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def with_sem(pe):
        async with sem:
            return await _parse_one_page(
                client,
                project_id=project_id,
                document_id=document_id,
                page_extraction=pe,
                source_path=source_path_str,
            )

    results = await asyncio.gather(*(with_sem(pe) for pe in pe_rows))
    total_revs = sum(len(r[0]) for r in results)
    total_cost = sum(r[1] for r in results)
    log.info(
        "revision_block: %s — %d pages parsed, %d revisions, $%.4f",
        document_id, len(pe_rows), total_revs, total_cost,
    )
    return {
        "document_id": document_id,
        "pages": len(pe_rows),
        "revisions": total_revs,
        "cost_usd": total_cost,
    }


async def parse_for_project(project_id: str) -> dict:
    """Run the revision-block parser against every drawing-set in a project."""
    async with SessionLocal() as db:
        docs = (
            await db.execute(
                select(Document)
                .where(Document.project_id == project_id)
                .where(Document.doc_type == "drawing-set")
            )
        ).scalars().all()

    total_pages = 0
    total_revs = 0
    total_cost = 0.0
    for doc in docs:
        try:
            r = await parse_for_document(doc.id)
            total_pages += r["pages"]
            total_revs += r["revisions"]
            total_cost += r["cost_usd"]
        except Exception as e:  # noqa: BLE001
            log.exception(
                "revision_block: failed on %s: %s", doc.filename, e
            )
    return {
        "project_id": project_id,
        "documents": len(docs),
        "pages_parsed": total_pages,
        "revisions": total_revs,
        "cost_usd": total_cost,
    }
