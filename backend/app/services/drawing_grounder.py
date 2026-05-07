"""Drawing grounder — adds where/count evidence to items by querying
Sonnet vision against tiled drawing-sheet renders.

Architecture (decided after the A/B test in scripts/ab_tiled_vision.py):
  - For each non-admin item from section_extractor, identify the candidate
    drawing sheets (via SheetIndex + discipline_config — discipline owning
    the item's CSI division).
  - Group items by sheet so one render serves many queries.
  - Per sheet: render at 300 DPI, split into 4 quadrants. Per quadrant,
    ask Sonnet vision to find every item in the batch. Run all 4
    quadrants in parallel.
  - Aggregate per-item: sum counts across quadrants, union locations,
    union callouts. The aggregated DrawingEvidence is written back as a
    synthetic supporting_chunk on the candidate so persistence routes it
    through the existing drawing-citation path.

Cost on a typical project (~30 sheets, ~5-15 items grouped per sheet):
  ~30 sheets × 4 quadrants = 120 vision calls × ~$0.005 ≈ $0.60.
  With concurrency=8 and ~5-10s per call, wall-clock is ~5-10 minutes.

Items that vision says "not found" simply don't get drawing evidence —
the downstream evidence_pattern rollup will mark them as missing-drawing
when they expected bilateral. That's correct behavior, not a failure.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import fitz  # PyMuPDF
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Chunk, Document, PageExtraction, SheetIndex
from .citation_validator import validate_citation
from .discipline_config import discipline_for_division
from .llm_log import record_call, usage_from_anthropic
from .retriever import RetrievedChunk
from .scope_extractor import CandidateItem

log = logging.getLogger(__name__)


_GROUNDER_CONCURRENCY = 4    # max sheets in flight; each launches 4 vision calls
_DPI = 300
_MAX_ITEMS_PER_PROMPT = 12   # batch size — keeps per-call output bounded
_MIN_OVERLAP_FOR_DRAWING_CITE = 0.05  # vision callouts share fewer specific tokens than spec text; be lenient


@dataclass
class DrawingEvidence:
    """What vision found for one item on one sheet."""
    sheet_id: str
    page_number: int
    chunk_id: str | None     # the existing page_summary chunk for this sheet
    count: int = 0
    locations: list[str] = field(default_factory=list)   # ["Restroom 116", ...]
    callouts: list[str] = field(default_factory=list)    # ["WC2 tag near east wall"]
    confidence: float = 0.0


@dataclass
class DrawingGrounderResult:
    items_total: int = 0
    items_grounded: int = 0     # got at least one drawing evidence
    items_skipped_admin: int = 0   # item_type was admin/qc
    items_unfound: int = 0      # tried but vision returned nothing
    sheets_queried: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


_GROUNDER_TOOL = {
    "name": "report_grounding",
    "description": (
        "For each item asked about, report whether it's visible in this "
        "specific quadrant of the drawing, where, and how many. Be strict — "
        "only mark found=true when the symbol/tag/callout for the item is "
        "actually visible in this quadrant. Do NOT extrapolate from legend "
        "definitions or assume an item is present because the section "
        "mentions it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_index": {
                            "type": "integer",
                            "description": "Index from the item list (0-based).",
                        },
                        "found": {
                            "type": "boolean",
                            "description": "True iff the item is actually depicted in this quadrant.",
                        },
                        "count": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Number of distinct instances visible in this quadrant.",
                        },
                        "locations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Room name/number per instance (e.g. 'Restroom 116'). Empty if not in a labeled room.",
                        },
                        "callout": {
                            "type": ["string", "null"],
                            "description": "Brief on-plan callout text or symbol description.",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["item_index", "found", "count", "confidence"],
                },
            }
        },
        "required": ["results"],
    },
    "cache_control": {"type": "ephemeral"},
}


def _get_client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _render_at_dpi(pdf_path: Path, page_number: int, dpi: int = _DPI) -> bytes:
    """PyMuPDF render of one page to PNG bytes."""
    with fitz.open(pdf_path) as pdf:
        page = pdf[page_number - 1]
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")


def _split_quadrants(png_bytes: bytes) -> dict[str, bytes]:
    """Split image into 4 quadrants (TL, TR, BL, BR)."""
    img = Image.open(io.BytesIO(png_bytes))
    w, h = img.size
    crops = {
        "TL": img.crop((0, 0, w // 2, h // 2)),
        "TR": img.crop((w // 2, 0, w, h // 2)),
        "BL": img.crop((0, h // 2, w // 2, h)),
        "BR": img.crop((w // 2, h // 2, w, h)),
    }
    out: dict[str, bytes] = {}
    for label, im in crops.items():
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        out[label] = buf.getvalue()
    return out


@dataclass
class _SheetMeta:
    sheet_id: str
    page_number: int
    title: str | None
    discipline: str | None
    chunk_id: str | None    # the page_summary chunk for this sheet (for citation)


async def _gather_sheet_metadata(
    db: AsyncSession, project_id: str
) -> tuple[Path, dict[str, _SheetMeta]]:
    """Pull drawing-set PDF path + per-sheet metadata in one round trip."""
    d = (
        await db.execute(
            select(Document)
            .where(Document.project_id == project_id)
            .where(Document.doc_type == "drawing-set")
            .order_by(Document.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if d is None:
        raise RuntimeError(f"no drawing-set document for project {project_id}")
    pdf_path = Path("data/uploads") / d.storage_path

    # SheetIndex: canonical (sheet_id, page_number, title, discipline)
    sheets = (
        await db.execute(
            select(SheetIndex)
            .where(SheetIndex.project_id == project_id)
        )
    ).scalars().all()

    # Map sheet_id → page_number via PageExtraction
    pages = (
        await db.execute(
            select(PageExtraction.sheet_number, PageExtraction.page_number)
            .where(PageExtraction.document_id == d.id)
        )
    ).all()
    sheet_to_page = {sn: pn for sn, pn in pages if sn}

    # One chunk per sheet (page_summary or page_text on that page) for citation.
    # Pick the page_summary if it exists, else any chunk on that page.
    chunks = (
        await db.execute(
            select(Chunk.id, Chunk.page_number, Chunk.chunk_type)
            .where(Chunk.project_id == project_id)
            .where(Chunk.document_id == d.id)
        )
    ).all()
    by_page: dict[int, str] = {}
    for cid, pn, ctype in chunks:
        if pn is None:
            continue
        if pn not in by_page or ctype == "page_summary":
            by_page[pn] = cid

    out: dict[str, _SheetMeta] = {}
    for s in sheets:
        page_num = sheet_to_page.get(s.sheet_id)
        if page_num is None:
            continue
        out[s.sheet_id] = _SheetMeta(
            sheet_id=s.sheet_id,
            page_number=page_num,
            title=s.title,
            discipline=s.discipline,
            chunk_id=by_page.get(page_num),
        )
    return pdf_path, out


def _eligible_items(items: Iterable[CandidateItem]) -> list[CandidateItem]:
    """Filter to items whose item_type expects drawing evidence."""
    out: list[CandidateItem] = []
    for it in items:
        item_type = getattr(it, "_item_type", None)
        if item_type in ("admin", "qc"):
            continue
        out.append(it)
    return out


def _sheets_for_item(
    item: CandidateItem, sheets_by_id: dict[str, _SheetMeta]
) -> list[_SheetMeta]:
    """Pick the sheets where this item's CSI division could appear.

    Strategy: discipline lookup via discipline_config. Item's CSI division
    → discipline. Match any sheet tagged with that discipline. Also include
    sheets whose discipline is None (e.g., 'general' sheets that the
    SheetIndex may not have classified).
    """
    div = (item.csi_code or "")[:2].zfill(2)
    discipline = discipline_for_division(div)
    if discipline is None:
        return []
    target_keys = {discipline.key}
    # Architectural items often appear on interior sheets too (and vice versa)
    if discipline.key == "architectural":
        target_keys.add("interior")
    elif discipline.key == "interior":
        target_keys.add("architectural")
    # Civil items often appear on site sheets
    if discipline.key == "civil":
        target_keys.add("site")
    return [s for s in sheets_by_id.values() if s.discipline in target_keys]


def _format_items_block(items: list[CandidateItem]) -> str:
    """Pack items into a numbered list for the vision prompt."""
    lines: list[str] = []
    for i, it in enumerate(items):
        lines.append(f"{i}. {it.description[:160]}")
    return "\n".join(lines)


async def _query_quadrant(
    client,
    project_id: str,
    sheet: _SheetMeta,
    quadrant: str,
    png_bytes: bytes,
    items_batch: list[CandidateItem],
) -> tuple[list[dict], int]:
    """One vision call. Returns (results, latency_ms)."""
    img_b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
    items_block = _format_items_block(items_batch)
    prompt = (
        f"You are looking at the {quadrant} quadrant of construction drawing "
        f"sheet {sheet.sheet_id} ({sheet.title or 'untitled'}, "
        f"discipline={sheet.discipline or '?'}).\n\n"
        f"For each item below, decide whether it is actually depicted in "
        f"THIS QUADRANT of the drawing. Be strict — only report found=true "
        f"when you can see the symbol, tag, or callout for the item in this "
        f"specific quadrant. Do NOT infer from legends or section text — "
        f"only what the plan actually shows.\n\n"
        f"=== ITEMS TO CHECK ({len(items_batch)}) ===\n{items_block}\n\n"
        f"Use report_grounding. Return one entry per item using the index "
        f"shown above. For found items, include count + room location(s) + "
        f"a short callout description."
    )
    t0 = time.perf_counter()
    msg = await client.messages.create(
        model=settings.vision_model,
        max_tokens=4096,
        tools=[_GROUNDER_TOOL],
        tool_choice={"type": "tool", "name": "report_grounding"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": img_b64,
                }},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict | None = None
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "report_grounding":
            payload = block.input
            break

    # Cost log
    from ..database import SessionLocal
    async with SessionLocal() as db:
        await record_call(
            db,
            purpose="drawing-grounder",
            model=settings.vision_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
        )
        await db.commit()

    if payload is None:
        return [], latency_ms
    return payload.get("results") or [], latency_ms


def _aggregate_quadrants(
    quadrant_results: list[list[dict]],
    items_batch: list[CandidateItem],
    sheet: _SheetMeta,
) -> dict[int, DrawingEvidence]:
    """Combine 4 quadrants of vision results into one DrawingEvidence per
    item index that was found anywhere in the sheet."""
    out: dict[int, DrawingEvidence] = {}
    for results in quadrant_results:
        for r in results:
            if not isinstance(r, dict):
                continue
            idx = r.get("item_index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(items_batch):
                continue
            if not r.get("found"):
                continue
            count = int(r.get("count") or 0)
            if count <= 0:
                continue
            confidence = float(r.get("confidence") or 0.0)
            ev = out.get(idx)
            if ev is None:
                ev = DrawingEvidence(
                    sheet_id=sheet.sheet_id,
                    page_number=sheet.page_number,
                    chunk_id=sheet.chunk_id,
                    confidence=confidence,
                )
                out[idx] = ev
            ev.count += count
            for loc in r.get("locations") or []:
                if isinstance(loc, str) and loc and loc not in ev.locations:
                    ev.locations.append(loc)
            callout = r.get("callout")
            if isinstance(callout, str) and callout and callout not in ev.callouts:
                ev.callouts.append(callout)
            ev.confidence = max(ev.confidence, confidence)
    return out


def _make_synthetic_drawing_chunk(
    sheet: _SheetMeta,
    item: CandidateItem,
    evidence: DrawingEvidence,
    base_chunk: Chunk | None,
) -> Chunk:
    """Build an in-memory Chunk for the candidate's supporting_chunks.

    Reuses the sheet's page_summary chunk_id so persistence routes the
    citation as evidence_type='drawing' (via doc_type lookup). The text is
    the vision-derived grounding statement so the citation_validator can
    reasonably verify it overlaps the item description.
    """
    grounding_text = (
        f"On sheet {evidence.sheet_id} (page {evidence.page_number}): "
        f"{evidence.count} instance(s) of {item.description[:120]}"
        + (f" — locations: {', '.join(evidence.locations)}" if evidence.locations else "")
        + (f" — callout: {evidence.callouts[0][:120]}" if evidence.callouts else "")
    )
    if base_chunk is not None:
        return Chunk(
            id=base_chunk.id,
            project_id=base_chunk.project_id,
            document_id=base_chunk.document_id,
            page_id=base_chunk.page_id,
            page_number=base_chunk.page_number,
            chunk_type=base_chunk.chunk_type,
            source_id=base_chunk.source_id,
            text=grounding_text,
            contextualized_text=base_chunk.contextualized_text,
            extra={**(base_chunk.extra or {}), "drawing_grounding": True,
                   "vision_count": evidence.count,
                   "vision_locations": evidence.locations},
            bbox=base_chunk.bbox,
            embedded=base_chunk.embedded,
        )
    # No base chunk to derive identity from — best-effort placeholder
    return Chunk(
        id="grounding-" + (sheet.chunk_id or sheet.sheet_id),
        project_id="",
        document_id="",
        page_id=None,
        page_number=evidence.page_number,
        chunk_type="page_summary",
        source_id=None,
        text=grounding_text,
        contextualized_text=None,
        extra={"drawing_grounding": True, "vision_count": evidence.count},
        bbox=None,
        embedded=True,
    )


async def _ground_one_sheet(
    client,
    project_id: str,
    pdf_path: Path,
    sheet: _SheetMeta,
    items_on_sheet: list[CandidateItem],
    base_chunks_by_id: dict[str, Chunk],
    sem: asyncio.Semaphore,
) -> tuple[dict[str, DrawingEvidence], int]:
    """Render the sheet, query 4 quadrants, aggregate, return per-item
    DrawingEvidence keyed by item.id (using id() since CandidateItem isn't
    hashable by default)."""
    async with sem:
        png = await asyncio.to_thread(
            _render_at_dpi, pdf_path, sheet.page_number, _DPI,
        )
        quadrants = _split_quadrants(png)

        # Batch items if larger than _MAX_ITEMS_PER_PROMPT.
        per_item_evidence: dict[int, DrawingEvidence] = {}
        latency = 0
        for batch_start in range(0, len(items_on_sheet), _MAX_ITEMS_PER_PROMPT):
            batch = items_on_sheet[batch_start:batch_start + _MAX_ITEMS_PER_PROMPT]
            tasks = [
                _query_quadrant(client, project_id, sheet, q, png_q, batch)
                for q, png_q in quadrants.items()
            ]
            quad_results = await asyncio.gather(*tasks, return_exceptions=True)
            cleaned: list[list[dict]] = []
            for qr in quad_results:
                if isinstance(qr, Exception):
                    log.warning(
                        "drawing_grounder: %s — quadrant call failed: %s",
                        sheet.sheet_id, qr,
                    )
                    continue
                results, lat = qr
                cleaned.append(results)
                latency = max(latency, lat)
            agg = _aggregate_quadrants(cleaned, batch, sheet)
            for local_idx, ev in agg.items():
                # Map back to global item index by item identity (we used
                # the local batch index in vision; here we recover the
                # CandidateItem and use id() as the dict key).
                item = batch[local_idx]
                per_item_evidence[id(item)] = ev

    return per_item_evidence, latency


async def ground_items(
    project_id: str,
    items: list[CandidateItem],
) -> tuple[list[CandidateItem], DrawingGrounderResult]:
    """Add drawing evidence to each non-admin item in `items`.

    Mutates each grounded item's `supporting_chunks` to append a synthetic
    drawing chunk citation. Returns (items, result) — `items` is the same
    list object, modified in place. Items that weren't grounded keep their
    spec-only citations and will be marked missing-drawing by the
    evidence_pattern rollup.
    """
    result = DrawingGrounderResult(items_total=len(items))
    eligible = _eligible_items(items)
    result.items_skipped_admin = len(items) - len(eligible)
    if not eligible:
        return items, result

    client = _get_client()
    if client is None:
        log.warning("drawing_grounder: no ANTHROPIC_API_KEY — skipping")
        return items, result

    from ..database import SessionLocal
    async with SessionLocal() as db:
        pdf_path, sheets_by_id = await _gather_sheet_metadata(db, project_id)
        # Pull all relevant base chunks once, so we can build synthetic
        # drawing-grounding chunks that reuse a real chunk_id.
        chunk_ids = {s.chunk_id for s in sheets_by_id.values() if s.chunk_id}
        base_chunks_by_id: dict[str, Chunk] = {}
        if chunk_ids:
            chunks = (
                await db.execute(
                    select(Chunk).where(Chunk.id.in_(chunk_ids))
                )
            ).scalars().all()
            base_chunks_by_id = {c.id: c for c in chunks}

    # Group items by sheet
    sheet_to_items: dict[str, list[CandidateItem]] = {}
    for item in eligible:
        for sheet in _sheets_for_item(item, sheets_by_id):
            sheet_to_items.setdefault(sheet.sheet_id, []).append(item)

    if not sheet_to_items:
        log.info("drawing_grounder: no sheets matched any items' disciplines")
        return items, result

    log.info(
        "drawing_grounder: project %s — %d eligible items across %d sheets, "
        "%d sheet-item pairs",
        project_id, len(eligible), len(sheet_to_items),
        sum(len(v) for v in sheet_to_items.values()),
    )

    sem = asyncio.Semaphore(_GROUNDER_CONCURRENCY)
    t0 = time.perf_counter()

    async def _run(sheet_id: str, items_on_sheet: list[CandidateItem]):
        sheet = sheets_by_id[sheet_id]
        return await _ground_one_sheet(
            client, project_id, pdf_path, sheet,
            items_on_sheet, base_chunks_by_id, sem,
        )

    sheet_results = await asyncio.gather(
        *(_run(sid, lst) for sid, lst in sheet_to_items.items()),
        return_exceptions=True,
    )

    # Flatten — each item could have evidence from multiple sheets; collect
    # all evidence per item, attach as separate supporting_chunks.
    grounded_count = 0
    for sheet_result in sheet_results:
        if isinstance(sheet_result, Exception):
            log.warning("drawing_grounder: sheet failed: %s", sheet_result)
            continue
        per_item_ev, _ = sheet_result
        for item_id, ev in per_item_ev.items():
            # Find the actual CandidateItem
            item = next((i for i in eligible if id(i) == item_id), None)
            if item is None:
                continue
            base = base_chunks_by_id.get(ev.chunk_id) if ev.chunk_id else None
            synthetic = _make_synthetic_drawing_chunk(
                sheets_by_id[ev.sheet_id], item, ev, base,
            )
            # Validate — though we use a lower threshold for vision-derived text
            verdict = validate_citation(
                item.description, synthetic.text or "",
                min_overlap=_MIN_OVERLAP_FOR_DRAWING_CITE,
            )
            if not verdict.valid:
                log.info(
                    "drawing_grounder: dropping low-overlap synthetic cite "
                    "for item=%r reason=%s",
                    item.description[:50], verdict.reason,
                )
                continue
            item.supporting_chunks.append(RetrievedChunk(
                chunk=synthetic,
                dense_score=1.0,
                sparse_score=1.0,
                rrf_score=1.0,
                rerank_score=ev.confidence,
                snippet=(synthetic.text or "")[:240],
            ))

    # Final stats
    grounded_set: set[int] = set()
    for sheet_result in sheet_results:
        if isinstance(sheet_result, Exception):
            continue
        per_item_ev, _ = sheet_result
        grounded_set.update(per_item_ev.keys())
    result.items_grounded = len(grounded_set)
    result.items_unfound = len(eligible) - result.items_grounded
    result.sheets_queried = len(sheet_to_items)
    result.latency_ms = int((time.perf_counter() - t0) * 1000)

    log.info(
        "drawing_grounder: %d/%d items grounded, %d unfound, %d sheets "
        "queried in %d ms",
        result.items_grounded, len(eligible), result.items_unfound,
        result.sheets_queried, result.latency_ms,
    )
    return items, result
