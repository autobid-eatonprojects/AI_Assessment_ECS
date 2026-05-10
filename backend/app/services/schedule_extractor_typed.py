"""P2 — per-schedule-type Sonnet vision extractor.

Pipeline (called for each schedule the schedule_router flagged on a page):
  1. crop_region(): render the bbox at 600 DPI as a tile crop
     (clamped to 8000-px max dim per Anthropic's vision cap)
  2. Sonnet 4.6 vision call with the type-specific Pydantic schema as
     the tool input_schema → forces the model to emit rows shaped like
     the AEC convention for that type
  3. Persist as ExtractedSchedule rows with extractor='typed_v1' and
     schedule_type='door' / etc. so downstream consumers can prefer the
     typed extraction over the generic fallback

The existing generic vision_extractor stays operational — typed
extractions augment, not replace. The schedule_miner pre-pass that feeds
scope_extractor reads from ExtractedSchedule and gets BOTH generic and
typed rows; the typed rows have higher confidence by construction (the
schema constrains hallucination) so downstream priorities them.

Cost: ~$0.05-0.10 per schedule × ~10-30 schedules per project = ~$1-3.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Document, DocumentPage, ExtractedSchedule, PageExtraction
from . import schedule_schemas
from .llm_log import record_call, usage_from_anthropic
from .renderer import crop_region
from .storage import storage

log = logging.getLogger(__name__)


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


def _make_tool(schedule_type: str, schema: dict) -> dict:
    return {
        "name": f"extract_{schedule_type}_schedule",
        "description": (
            f"Extract every row from the {schedule_type} schedule visible "
            "in the cropped image. Each row corresponds to one biddable "
            "item (one door, one window, one fixture, etc.). Use the "
            "schema columns where they apply; columns the schedule has "
            "but the schema didn't anticipate go in extra_fields."
        ),
        "input_schema": schema,
        "cache_control": {"type": "ephemeral"},
    }


def _system_prompt_for(schedule_type: str) -> str:
    return f"""\
You are extracting one {schedule_type} schedule from a high-resolution \
crop of an architectural / engineering drawing.

Rules:
  1. Emit ONE ROW per row visible in the schedule. Do not summarise. If
     you see 35 rows, emit 35 rows.
  2. Preserve VERBATIM what's in the cell. Don't normalise units, don't
     convert "3'-0\\"" to "36 in", don't expand "MTL" to "metal".
  3. Use the named columns where they apply. If the schedule has a column
     the schema doesn't name (e.g. a custom "By GC" column), put it in
     extra_fields with the printed header as the key.
  4. If a cell is blank or "—" or "N/A", leave it null.
  5. The mark / tag / room number / panel ID column is REQUIRED for every
     row. If you can't read it, skip the row rather than guessing.

Use extract_{schedule_type}_schedule now.
"""


@dataclass
class _RoutedRequest:
    page_extraction_id: str
    page_id: str
    page_number: int
    document_id: str
    project_id: str
    source_path_str: str
    schedule_type: str
    name: str
    bbox: dict


@dataclass
class TypedExtractionResult:
    schedule_type: str
    rows_extracted: int
    cost_usd: float
    latency_ms: int


async def extract_one_typed(
    req: _RoutedRequest, *, dpi: int = 600
) -> TypedExtractionResult | None:
    """Run one typed extraction (one schedule on one page)."""
    schema = schedule_schemas.schema_for(req.schedule_type)
    if schema is None:
        # 'other' type or unknown — skip the typed pass; the generic
        # extractor's data still covers it.
        return None

    client = _get_client()
    if client is None:
        return None

    # Render the bbox region at high DPI
    from pathlib import Path

    source = storage.absolute_path(req.source_path_str)
    bbox_tuple = (
        float(req.bbox.get("x0") or 0.0),
        float(req.bbox.get("y0") or 0.0),
        float(req.bbox.get("x1") or 1.0),
        float(req.bbox.get("y1") or 1.0),
    )
    try:
        crop_path = await crop_region(
            source, req.document_id, req.page_number, bbox_tuple, dpi=dpi
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "schedule_extractor_typed: crop failed for page %d (%s): %s",
            req.page_number, req.schedule_type, e,
        )
        return None

    # Auto-downsamples to JPEG if the crop exceeds Anthropic's 5MB cap.
    from .renderer import read_image_for_vision

    raw, media_type = read_image_for_vision(Path(crop_path))
    b64 = base64.standard_b64encode(raw).decode("ascii")

    tool = _make_tool(req.schedule_type, schema)
    system = _system_prompt_for(req.schedule_type)

    t0 = time.perf_counter()
    try:
        msg = await client.messages.create(
            model=settings.vision_model,
            max_tokens=16384,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
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
                            "text": f"Extract the {req.schedule_type} schedule from this crop.",
                        },
                    ],
                }
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "schedule_extractor_typed: API error on page %d %s: %s",
            req.page_number, req.schedule_type, e,
        )
        return None
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == tool["name"]
        ):
            payload = block.input
            break

    rows = payload.get("rows") or []
    schedule_name = payload.get("schedule_name_as_printed") or req.name

    cost: float = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose=f"schedule-typed-{req.schedule_type}",
            model=settings.vision_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=req.project_id,
            document_id=req.document_id,
            page_extraction_id=req.page_extraction_id,
        )
        cost = c or 0.0

        # Persist as a new ExtractedSchedule row alongside the generic
        # extraction. Columns: rebuild from the row keys; rows: passthrough.
        # This row's `name` carries the typed extractor marker so downstream
        # consumers can prefer it over the generic.
        column_set: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for k in row.keys():
                if k != "extra_fields" and k not in seen:
                    seen.add(k)
                    column_set.append(k)

        db.add(
            ExtractedSchedule(
                page_extraction_id=req.page_extraction_id,
                name=f"[typed:{req.schedule_type}] {schedule_name}",
                columns=column_set,
                rows=rows,
                bbox=req.bbox,
            )
        )
        await db.commit()

    log.info(
        "schedule_extractor_typed: page %d %s — %d rows in %dms ($%.4f)",
        req.page_number, req.schedule_type, len(rows), latency_ms, cost,
    )
    return TypedExtractionResult(
        schedule_type=req.schedule_type,
        rows_extracted=len(rows),
        cost_usd=cost,
        latency_ms=latency_ms,
    )


async def extract_for_routed_schedules(routed_schedules: list) -> dict:
    """Run typed extraction over a router result's schedule list.

    `routed_schedules` is a list of `_RoutedSchedule` objects from
    schedule_router.RouteResult.schedules. We need the source PDF path
    per document, and a PageExtraction row per page (so the new
    ExtractedSchedule rows have a parent FK).
    """
    if not routed_schedules:
        return {"requests": 0, "rows": 0, "cost_usd": 0.0}

    # Group by document so we only look up source paths once per doc
    by_doc: dict[str, list] = {}
    for r in routed_schedules:
        by_doc.setdefault(r.document_id, []).append(r)

    requests: list[_RoutedRequest] = []
    async with SessionLocal() as db:
        for document_id, group in by_doc.items():
            doc = await db.get(Document, document_id)
            if doc is None:
                continue
            page_numbers = {r.page_number for r in group}
            page_extractions = (
                await db.execute(
                    select(PageExtraction)
                    .where(PageExtraction.document_id == document_id)
                    .where(PageExtraction.page_number.in_(page_numbers))
                )
            ).scalars().all()
            pe_by_page = {pe.page_number: pe for pe in page_extractions}
            for r in group:
                pe = pe_by_page.get(r.page_number)
                if pe is None:
                    log.warning(
                        "schedule_extractor_typed: no PageExtraction for "
                        "page %d in %s — skipping typed extract",
                        r.page_number, document_id,
                    )
                    continue
                requests.append(
                    _RoutedRequest(
                        page_extraction_id=pe.id,
                        page_id=r.page_id,
                        page_number=r.page_number,
                        document_id=document_id,
                        project_id=doc.project_id,
                        source_path_str=doc.storage_path,
                        schedule_type=r.schedule_type,
                        name=r.name,
                        bbox=r.bbox,
                    )
                )

    import asyncio as _asyncio

    sem = _asyncio.Semaphore(4)

    async def with_sem(req):
        async with sem:
            return await extract_one_typed(req)

    results = await _asyncio.gather(*(with_sem(r) for r in requests))

    rows_total = 0
    cost_total = 0.0
    by_type: dict[str, int] = {}
    for r in results:
        if r is None:
            continue
        rows_total += r.rows_extracted
        cost_total += r.cost_usd
        by_type[r.schedule_type] = by_type.get(r.schedule_type, 0) + r.rows_extracted

    return {
        "requests": len(requests),
        "extracted": sum(1 for r in results if r is not None),
        "rows": rows_total,
        "cost_usd": cost_total,
        "by_type": by_type,
    }
