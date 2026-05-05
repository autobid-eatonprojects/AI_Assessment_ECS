"""Phase 4.3 Stage 0 — Schedule Miner pre-pass.

The EVE multi-query extractor (Stage A) tends to *summarize* large schedules
("Hollow metal doors per A2.1") rather than enumerate them per row. For
schedule-row line items the audit showed ~30% recall — 38 individual doors
collapsed to 3 generic types, 21 lighting fixtures missed entirely.

This pre-pass walks every Phase-2 ExtractedSchedule and asks Haiku to:
  1. Decide if the schedule is quantifiable (each row = one biddable item).
     Many schedules are reference data (legend, abbreviation, code-table,
     panel-totals) and should be skipped.
  2. Map the schedule to a CSI division (NN format).
  3. Emit one structured row → CandidateItem.

Each candidate is tagged extraction_method='schedule_miner' and cites the
schedule's existing Chunk row, so deep-linking and bbox grounding still work.
Schedule-miner candidates skip Stage B validation: they come from
deterministic Phase-2 structured data, not from a generative summarization
that could hallucinate. Confidence is fixed at 1.0 (votes=[]).

Cost: ~$0.005 per schedule × ~50 quantifiable schedules = ~$0.25.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings
from ..models import Chunk, ExtractedSchedule, PageExtraction
from .llm_log import Usage, record_call, usage_from_anthropic
from .retriever import RetrievedChunk
from .scope_extractor import CandidateItem
from .trade_list_parser import CSITaxonomy

log = logging.getLogger(__name__)


_MINER_CONCURRENCY = 8


# Tool schema — one Haiku call per schedule. The model decides:
#   - Is each row a biddable item? (else skip)
#   - Which CSI division does this schedule belong to?
#   - What is the row-level description, quantity, unit, spec?
_MINE_TOOL = {
    "name": "mine_schedule",
    "description": (
        "Inspect a structured schedule extracted from a construction drawing. "
        "Decide whether each row represents one biddable scope item; if so, "
        "emit one item per row with a concise description. Skip schedules "
        "that are reference data (legends, abbreviations, calculation sheets, "
        "panel totals, code tables, lumber-grade tables, etc.) by setting "
        "is_quantifiable=false and items=[]."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_quantifiable": {
                "type": "boolean",
                "description": (
                    "True if each row of this schedule represents a discrete, "
                    "biddable item that a subcontractor would price separately "
                    "(doors, lighting fixtures, plumbing fixtures, footings, "
                    "etc.). False for legends, abbreviations, calculation "
                    "tables, code lookup tables, panel-totals rows, etc."
                ),
            },
            "csi_division": {
                "type": "string",
                "description": (
                    "Best-fit CSI division as 'NN'. Doors/Windows → '08'. "
                    "Lighting / Panels → '26'. Plumbing fixtures → '22'. "
                    "HVAC equipment → '23'. Footings/Concrete → '03'. "
                    "Restroom/Bath accessories → '10'. Finishes → '09'. "
                    "Required when is_quantifiable=true."
                ),
            },
            "csi_section": {
                "type": "string",
                "description": (
                    "Optional 6-digit CSI section as 'NN NN NN' if "
                    "confidently known. Else null."
                ),
            },
            "default_unit": {
                "type": "string",
                "description": (
                    "Default unit for items in this schedule when row doesn't "
                    "state one (EA, LF, SF, CY, lbs). Use 'EA' for discrete "
                    "fixtures/equipment. Required when is_quantifiable=true."
                ),
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "row_index": {
                            "type": "integer",
                            "description": (
                                "0-based index of the source row in the "
                                "schedule.rows array."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "One-line scope description for this row. "
                                "Lead with the mark/tag/designation if any "
                                "(e.g. 'Door 100: 6'-0\" × 8'-7\" pair, "
                                "aluminum/glass'). Be concise but specific "
                                "enough to bid against."
                            ),
                        },
                        "quantity": {
                            "type": "string",
                            "description": (
                                "Quantity from the row if stated; else '1' "
                                "(each row = one item by default)."
                            ),
                        },
                        "unit": {
                            "type": "string",
                            "description": "Unit code; defaults to default_unit.",
                        },
                        "specification": {
                            "type": "string",
                            "description": (
                                "Material/manufacturer/model spec from the "
                                "row, if present. Else null."
                            ),
                        },
                    },
                    "required": ["row_index", "description"],
                },
            },
        },
        "required": ["is_quantifiable", "items"],
    },
}


@dataclass
class _ScheduleContext:
    """Bundle of everything we need for one schedule's Haiku call + persistence."""

    schedule: ExtractedSchedule
    chunk: Chunk | None  # The Phase-3 chunk for this schedule (cited by candidates)
    sheet_number: str | None
    page_number: int | None


def _get_client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _format_schedule_for_prompt(s: ExtractedSchedule, sheet: str | None) -> str:
    """Render the schedule as compact text for the Haiku call.

    We include row indices so the model can reference rows by index in its
    output (avoiding ambiguous text identifiers).
    """
    parts = [f"Schedule name: {s.name}"]
    if sheet:
        parts.append(f"Sheet: {sheet}")
    parts.append(f"Columns: {' | '.join(s.columns)}")
    parts.append("Rows:")
    for i, row in enumerate(s.rows):
        cells = []
        for c in s.columns:
            v = row.get(c, "")
            if v not in (None, ""):
                cells.append(f"{c}={v}")
        parts.append(f"  [{i}] " + "; ".join(cells))
    return "\n".join(parts)


def _build_synthetic_row_chunk(
    base_chunk: Chunk,
    schedule_name: str,
    row_index: int,
    row: dict,
    columns: list[str],
) -> Chunk:
    """Synthetic Chunk: a copy of the schedule chunk whose text is just one row.

    Used as the supporting_chunk for a per-row CandidateItem. The .id, .bbox,
    .page_id etc. all reference the real schedule chunk (so citations and
    deep-linking work), but .text is the single row so downstream prompts
    aren't truncated unfairly. The synthetic Chunk is never persisted — it's
    just an in-memory carrier.
    """
    cells = "; ".join(
        f"{c}={row.get(c, '')}" for c in columns if row.get(c) not in (None, "")
    )
    row_text = f"From {schedule_name} (row {row_index + 1}): {cells}"
    # Build a detached Chunk instance with the same identity but row-scoped text.
    # We don't persist this — it lives only as long as the validation/citation
    # references it. Citations write back to the real chunk_id.
    synthetic = Chunk(
        id=base_chunk.id,
        project_id=base_chunk.project_id,
        document_id=base_chunk.document_id,
        page_id=base_chunk.page_id,
        page_number=base_chunk.page_number,
        chunk_type=base_chunk.chunk_type,
        source_id=base_chunk.source_id,
        text=row_text,
        contextualized_text=base_chunk.contextualized_text,
        extra=base_chunk.extra,
        bbox=base_chunk.bbox,
        embedded=base_chunk.embedded,
    )
    return synthetic


async def _mine_one_schedule(
    client,
    project_id: str,
    ctx: _ScheduleContext,
    taxonomy: CSITaxonomy,
) -> tuple[list[CandidateItem], Usage, int]:
    """Run the Haiku miner on a single schedule. Returns (candidates, usage, latency_ms)."""
    s = ctx.schedule
    if not s.rows:
        return [], Usage(), 0

    if ctx.chunk is None:
        log.warning(
            "schedule_miner: no chunk for schedule %s — skipping (rerun indexer?)",
            s.id,
        )
        return [], Usage(), 0

    prompt_body = _format_schedule_for_prompt(s, ctx.sheet_number)
    prompt = (
        f"{prompt_body}\n\n"
        "Decide whether each row is a biddable scope item. If yes, emit one "
        "items[] entry per row. If the schedule is reference data (legend, "
        "abbreviation table, calculation sheet, panel totals, code lookup, "
        "lumber/material grade table, etc.) set is_quantifiable=false and "
        "items=[]. Use the mine_schedule tool now."
    )

    t0 = time.perf_counter()
    msg = await client.messages.create(
        model=settings.classifier_model,  # Haiku 4.5
        max_tokens=4096,
        tools=[_MINE_TOOL],
        tool_choice={"type": "tool", "name": "mine_schedule"},
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict | None = None
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "mine_schedule"
        ):
            payload = block.input
            break
    if payload is None:
        log.warning("schedule_miner: no tool_use for schedule %s", s.id)
        return [], usage_from_anthropic(msg), latency_ms

    if not payload.get("is_quantifiable"):
        return [], usage_from_anthropic(msg), latency_ms

    division = (payload.get("csi_division") or "").strip()
    if not division or len(division) < 2:
        log.warning(
            "schedule_miner: missing csi_division for %s — skipping", s.name
        )
        return [], usage_from_anthropic(msg), latency_ms

    # Build CSI code: prefer the section if model gave one, else division-level.
    section = (payload.get("csi_section") or "").strip()
    csi_code = section if section else f"{division[:2]} 00 00"

    default_unit = (payload.get("default_unit") or "EA").strip() or "EA"
    candidates: list[CandidateItem] = []

    for it in payload.get("items", []):
        row_index = int(it.get("row_index", -1))
        if row_index < 0 or row_index >= len(s.rows):
            continue
        description = (it.get("description") or "").strip()
        if not description:
            continue
        quantity = it.get("quantity") or "1"
        unit = (it.get("unit") or default_unit).strip() or default_unit
        specification = it.get("specification")

        synthetic_chunk = _build_synthetic_row_chunk(
            ctx.chunk,
            schedule_name=s.name,
            row_index=row_index,
            row=s.rows[row_index],
            columns=s.columns,
        )
        # Wrap as RetrievedChunk so downstream code (validator, persister)
        # doesn't need to special-case schedule-miner candidates.
        retrieved = RetrievedChunk(
            chunk=synthetic_chunk,
            dense_score=1.0,
            sparse_score=1.0,
            rrf_score=1.0,
            rerank_score=1.0,
            snippet=synthetic_chunk.text[:240],
        )

        cand = CandidateItem(
            csi_code=csi_code,
            description=description,
            specification=specification,
            quantity=str(quantity),
            unit=unit,
            location=None,
            extraction_method="schedule_miner",
            source_chunk_ids=[ctx.chunk.id],
            found_by_query="schedule_miner",
            supporting_chunks=[retrieved],
        )
        candidates.append(cand)

    return candidates, usage_from_anthropic(msg), latency_ms


async def mine_schedules(
    project_id: str, taxonomy: CSITaxonomy
) -> tuple[dict[str, list[CandidateItem]], float]:
    """Walk every ExtractedSchedule for the project and emit per-row candidates.

    Returns:
        ({csi_division: [CandidateItem]}, total_cost_usd)

    The returned dict is keyed by 2-digit CSI division (e.g. "03", "08", "26")
    so the orchestrator can merge schedule candidates with EVE candidates for
    each division before dedupe.
    """
    from ..database import SessionLocal

    client = _get_client()
    if client is None:
        log.warning("schedule_miner: no ANTHROPIC_API_KEY — skipping pre-pass")
        return {}, 0.0

    # Pull every ExtractedSchedule for the project, joined to its page (for
    # sheet number) and its Chunk (so we can cite it).
    async with SessionLocal() as db:
        # All schedules for project (via Document → PageExtraction)
        from ..models import Document

        schedules = (
            await db.execute(
                select(
                    ExtractedSchedule,
                    PageExtraction.sheet_number,
                    PageExtraction.page_number,
                )
                .join(
                    PageExtraction,
                    ExtractedSchedule.page_extraction_id == PageExtraction.id,
                )
                .join(Document, PageExtraction.document_id == Document.id)
                .where(Document.project_id == project_id)
                .where(Document.source == "project_document")
                .where(PageExtraction.status == "ready")
            )
        ).all()

        if not schedules:
            log.info("schedule_miner: no extracted schedules for project %s", project_id)
            return {}, 0.0

        # One chunk per schedule (chunk_type='schedule', source_id=schedule.id)
        schedule_ids = [s.id for s, _, _ in schedules]
        chunk_rows = (
            await db.execute(
                select(Chunk)
                .where(Chunk.project_id == project_id)
                .where(Chunk.chunk_type == "schedule")
                .where(Chunk.source_id.in_(schedule_ids))
            )
        ).scalars().all()
        chunk_by_schedule_id = {c.source_id: c for c in chunk_rows}

    contexts = [
        _ScheduleContext(
            schedule=s,
            chunk=chunk_by_schedule_id.get(s.id),
            sheet_number=sheet,
            page_number=page,
        )
        for s, sheet, page in schedules
    ]

    log.info(
        "schedule_miner: mining %d schedules for project %s",
        len(contexts),
        project_id,
    )

    sem = asyncio.Semaphore(_MINER_CONCURRENCY)

    async def run_one(ctx: _ScheduleContext):
        async with sem:
            try:
                return await _mine_one_schedule(client, project_id, ctx, taxonomy)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "schedule_miner: failed on schedule %r (%s): %s",
                    ctx.schedule.name,
                    ctx.schedule.id,
                    e,
                )
                return [], Usage(), 0

    results = await asyncio.gather(*(run_one(c) for c in contexts))

    # Persist cost via record_call, group candidates by division
    by_division: dict[str, list[CandidateItem]] = defaultdict(list)
    total_cost = 0.0
    async with SessionLocal() as db:
        for cands, usage, latency_ms in results:
            cost, _ = await record_call(
                db,
                purpose="schedule-miner",
                model=settings.classifier_model,
                usage=usage,
                latency_ms=latency_ms,
                project_id=project_id,
            )
            total_cost += cost or 0.0
            for c in cands:
                division = c.csi_code[:2]
                by_division[division].append(c)
        await db.commit()

    quantifiable = sum(1 for cands, _, _ in results if cands)
    log.info(
        "schedule_miner: %d/%d schedules quantifiable, %d candidates across %d divisions, $%.4f",
        quantifiable,
        len(contexts),
        sum(len(v) for v in by_division.values()),
        len(by_division),
        total_cost,
    )

    return dict(by_division), total_cost
