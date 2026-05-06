"""P3 — discipline agent (per design doc, 9-discipline parallelism).

ONE agent per discipline (architectural, structural, mechanical, etc.).
Each agent owns the CSI divisions in its bucket and runs the 4-step
protocol from the design doc:

    1. Read SPEC sections in this discipline's CSI divisions
       → emit `mandates[]` (what the spec REQUIRES)
    2. Read DRAWING sheets tagged with this discipline (from SheetIndex)
       → emit `work_items[]` (what's physically DRAWN)
       Plus: schedule_extractor_typed rows for this discipline
       Plus (FP/P/M/E): YOLO11 MEP symbols when available
    3. Cross-check mandates vs work_items
       → mandates without matching work_items → spec_without_drawing[]
       → work_items without matching mandates → drawing_without_spec[]
       Both kinds flow through to gap_detector as Gap rows
    4. Emit scope items with bilateral evidence enforced —
       every item carries ≥1 spec citation AND ≥1 drawing citation
       (or marked unilateral and flagged for HITL)

Discipline-coherent context (per design doc):
    Each discipline's call loads ~80K tokens of relevant material:
      - This discipline's spec sections (resolved via spec_toc subset)
      - This discipline's drawing chunks (filtered via SheetIndex.discipline)
      - This discipline's typed schedule rows
      - The project profile (small)
      - YOLO MEP symbols (FP/P/M/E only)
      - Project symbol legend (when ingested)

All loaded as a single Sonnet 4.6 call with prompt caching on the
discipline corpus. Re-running across multiple sub-tasks (e.g.
resolution of an HITL flagged item) reuses the cached corpus — that's
where the design doc's "85% input cost reduction" comes from.

Operates ALONGSIDE the existing per-CSI-division EVE extractor for now
— this isn't a destructive replacement. The orchestrator (scope_runner)
chooses which mode to run via config. Both modes' outputs land in the
same ScopeItem table; the discipline-agent rows are tagged
`extraction_method='discipline_agent_v1'` so downstream consumers can
prefer them when both exist.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import SessionLocal
from ..models import (
    Chunk,
    Document,
    ExtractedSchedule,
    PageExtraction,
    ProjectProfile,
    SheetIndex,
)
from .discipline_config import Discipline, discipline_for_division
from .llm_log import record_call, usage_from_anthropic

log = logging.getLogger(__name__)


# Sonnet 4.6's effective context window for one cached prompt.
_DISCIPLINE_CONTEXT_TOK_TARGET = 80_000


@dataclass
class _DisciplineCorpus:
    """The bundle of project material an agent reads in one call."""

    discipline: Discipline
    spec_chunks: list[Chunk] = field(default_factory=list)
    drawing_chunks: list[Chunk] = field(default_factory=list)
    typed_schedule_rows: list[dict] = field(default_factory=list)
    profile_summary: str = ""
    symbol_legend_summary: str = ""
    yolo_symbols: list[dict] = field(default_factory=list)
    sheet_manifest: list[dict] = field(default_factory=list)


@dataclass
class DisciplineAgentResult:
    discipline: str
    mandates_count: int
    work_items_count: int
    spec_without_drawing: list[dict]
    drawing_without_spec: list[dict]
    scope_items: list[dict]
    cost_usd: float
    latency_ms: int
    raw: dict


_AGENT_TOOL = {
    "name": "emit_discipline_scope",
    "description": (
        "Emit the discipline's full scope of work after running the "
        "4-step protocol: read spec → mandates, read drawings → work "
        "items, cross-check, emit. Every scope_item must cite at least "
        "one spec chunk_id AND at least one drawing chunk_id (bilateral "
        "evidence requirement). Items lacking one side go in "
        "unilateral_items with a reason. Mandates without a matching "
        "work_item go in spec_without_drawing; work_items without a "
        "matching mandate go in drawing_without_spec — both feed the "
        "gap detector."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mandates": {
                "type": "array",
                "description": (
                    "What the SPEC requires for this discipline. Each "
                    "mandate is a high-level requirement (e.g. 'all "
                    "concrete shall be 4000 PSI per ACI 318'). Cite the "
                    "spec chunk_ids that establish it."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "csi_section": {"type": "string"},
                        "summary": {"type": "string"},
                        "spec_chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["csi_section", "summary", "spec_chunk_ids"],
                },
            },
            "work_items": {
                "type": "array",
                "description": (
                    "What's physically DRAWN — schedule rows, plan "
                    "callouts, fixture counts, etc. Cite the drawing "
                    "chunk_ids."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": ["string", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "location": {"type": ["string", "null"]},
                        "drawing_chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["description", "drawing_chunk_ids"],
                },
            },
            "spec_without_drawing": {
                "type": "array",
                "description": "Mandates the spec set but you couldn't find drawing evidence for.",
                "items": {
                    "type": "object",
                    "properties": {
                        "csi_section": {"type": "string"},
                        "summary": {"type": "string"},
                        "spec_chunk_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["csi_section", "summary"],
                },
            },
            "drawing_without_spec": {
                "type": "array",
                "description": "Drawn items you couldn't find a spec mandate for.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "drawing_chunk_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["description"],
                },
            },
            "scope_items": {
                "type": "array",
                "description": (
                    "Final bilateral-evidence scope items — every entry "
                    "MUST have ≥1 spec_chunk_ids AND ≥1 drawing_chunk_ids. "
                    "Items lacking one side go in unilateral_items, not here."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "csi_code": {"type": "string", "description": "6-digit '03 30 00' format"},
                        "description": {"type": "string"},
                        "specification": {"type": ["string", "null"]},
                        "quantity": {"type": ["string", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "location": {"type": ["string", "null"]},
                        "spec_chunk_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "drawing_chunk_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": [
                        "csi_code", "description", "spec_chunk_ids",
                        "drawing_chunk_ids", "confidence",
                    ],
                },
            },
            "unilateral_items": {
                "type": "array",
                "description": (
                    "Items that look real but lack bilateral evidence. "
                    "Surface for HITL review rather than emitting as "
                    "first-class scope_items."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "csi_code": {"type": "string"},
                        "description": {"type": "string"},
                        "evidence_side": {
                            "type": "string",
                            "enum": ["spec_only", "drawing_only"],
                        },
                        "chunk_ids": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["csi_code", "description", "evidence_side", "chunk_ids"],
                },
            },
        },
        "required": ["mandates", "work_items", "scope_items"],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT_TEMPLATE = """\
You are the {discipline_label} discipline agent for an estimating \
pipeline. Your job: emit the FULL scope of work for this discipline by \
reading the project's spec sections + drawing sheets + schedule data + \
(if available) MEP symbol detections + the project's symbol legend.

Run the 4-step protocol:

  1. READ THE SPEC FIRST.
     For each spec section in this discipline's CSI divisions, identify
     the MANDATES — what the spec requires. Cite the spec chunk_ids.

  2. READ THE DRAWINGS NEXT.
     Identify physical WORK ITEMS — schedule rows, plan callouts,
     fixture counts, equipment tags. Cite the drawing chunk_ids.

  3. CROSS-CHECK.
     For each mandate, find the work_items that satisfy it. Mandates
     without matching work_items → spec_without_drawing.
     Work_items without matching mandates → drawing_without_spec.
     Both lists feed the project's Gap report.

  4. EMIT BILATERAL-EVIDENCE SCOPE ITEMS.
     The output `scope_items` MUST have ≥1 spec_chunk_id AND ≥1
     drawing_chunk_id per item. Items with only one side go in
     `unilateral_items` for HITL review — they're real but the
     evidence is one-sided.

Style rules:
  - Be EXHAUSTIVE on schedule rows. If the door schedule has 35 rows,
    emit 35 scope items.
  - Don't invent. Every item must cite chunk_ids you actually saw.
  - Use the canonical 6-digit CSI code ('NN NN NN' format) from the
    project's CSI subset. Don't fabricate codes.
  - Prefer wording from the spec / drawings VERBATIM. Don't normalize
    units or expand abbreviations.

Use emit_discipline_scope when you've completed all 4 steps. Don't
short-circuit — emitting without the cross-check step produces
unilateral items that the operator has to clean up.
"""


def _format_chunk_block(chunks: list[Chunk], side_label: str) -> str:
    """Render chunks as a labeled block the agent can read + cite."""
    parts: list[str] = []
    for c in chunks:
        meta = c.extra or {}
        sheet = meta.get("sheet_number") or meta.get("doc_type") or "—"
        page = c.page_number or "—"
        text = c.text or ""
        # Cap individual chunks to keep the prompt bounded
        if len(text) > 1500:
            text = text[:1500] + "…"
        parts.append(
            f"[{side_label} | chunk_id={c.id} | sheet={sheet} | page={page}]\n"
            f"{text}"
        )
    return "\n\n---\n\n".join(parts)


def _format_typed_schedule_block(rows: list[dict]) -> str:
    """Render typed schedule rows as a compact JSON block per row."""
    import json

    parts: list[str] = []
    for r in rows:
        # r already has schedule context
        parts.append(json.dumps(r, ensure_ascii=False))
    return "\n".join(parts)


async def gather_corpus_for_discipline(
    db: AsyncSession,
    project_id: str,
    discipline: Discipline,
) -> _DisciplineCorpus:
    """Pull every chunk + schedule + sheet relevant to one discipline."""
    corpus = _DisciplineCorpus(discipline=discipline)

    # Profile (project-wide; small)
    profile = (
        await db.execute(
            select(ProjectProfile).where(ProjectProfile.project_id == project_id)
        )
    ).scalar_one_or_none()
    if profile is not None:
        corpus.profile_summary = (
            f"Building type: {profile.building_type}; "
            f"Size: {profile.size_sf} SF; "
            f"Occupancy: {profile.occupancy}; "
            f"Construction type: {profile.construction_type}; "
            f"Sprinklered: {profile.sprinklered}; "
            f"Stories: {profile.stories}; "
            f"Codes: {profile.codes}; "
        )

    # Sheet manifest filtered to this discipline (via SheetIndex)
    sheet_rows = (
        await db.execute(
            select(SheetIndex)
            .where(SheetIndex.project_id == project_id)
            .where(SheetIndex.discipline == discipline.key)
        )
    ).scalars().all()
    corpus.sheet_manifest = [
        {
            "sheet_id": s.sheet_id,
            "title": s.title,
            "discipline": s.discipline,
        }
        for s in sheet_rows
    ]
    discipline_sheet_ids = {s.sheet_id for s in sheet_rows}

    # Drawing chunks for this discipline's sheets
    if discipline_sheet_ids:
        drawing_chunks_q = (
            select(Chunk)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.project_id == project_id)
            .where(Document.doc_type == "drawing-set")
        )
        all_drawing_chunks = (await db.execute(drawing_chunks_q)).scalars().all()
        # Filter by sheet_number in chunk.extra
        for c in all_drawing_chunks:
            extra = c.extra or {}
            sheet = extra.get("sheet_number")
            if sheet and sheet in discipline_sheet_ids:
                corpus.drawing_chunks.append(c)

    # Spec chunks: page_text chunks from written-spec docs that mention
    # one of this discipline's CSI divisions (cheap heuristic; the spec
    # toc subset would be better but works as fallback)
    spec_chunks_q = (
        select(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.project_id == project_id)
        .where(Document.doc_type == "written-spec")
    )
    all_spec_chunks = (await db.execute(spec_chunks_q)).scalars().all()
    div_set = set(discipline.csi_divisions)
    for c in all_spec_chunks:
        # Filter spec chunks by which divisions they mention. A chunk
        # mentioning "23 21 13" or any other matching code lands here.
        text = c.text or ""
        if any(f"{div} " in text or f"\n{div}" in text for div in div_set):
            corpus.spec_chunks.append(c)

    # Typed schedule rows for this discipline (filtered by where the
    # schedule type maps to this discipline — equipment & fixture →
    # mech/plumb, panel → electrical, door/window/finish/room →
    # architectural / interior)
    typed_schedule_q = (
        select(ExtractedSchedule)
        .join(PageExtraction, ExtractedSchedule.page_extraction_id == PageExtraction.id)
        .join(Document, PageExtraction.document_id == Document.id)
        .where(Document.project_id == project_id)
        .where(ExtractedSchedule.name.like("[typed:%"))
    )
    typed_schedules = (await db.execute(typed_schedule_q)).scalars().all()
    for s in typed_schedules:
        # Schedule.name format: "[typed:door] DOOR SCHEDULE"
        sched_type = (s.name or "").split("]")[0].split(":")[-1]
        if _schedule_type_belongs_to(sched_type, discipline.key):
            for row in (s.rows or []):
                corpus.typed_schedule_rows.append(
                    {
                        "schedule": s.name,
                        "schedule_id": s.id,
                        "page_extraction_id": s.page_extraction_id,
                        "row": row,
                    }
                )

    return corpus


def _schedule_type_belongs_to(schedule_type: str, discipline_key: str) -> bool:
    """Map schedule type → discipline (used to filter typed rows per agent)."""
    return {
        "door": discipline_key in ("architectural", "interior"),
        "window": discipline_key in ("architectural", "interior"),
        "finish": discipline_key == "interior",
        "room": discipline_key in ("architectural", "interior"),
        "fixture": discipline_key == "plumbing",
        "panel": discipline_key == "electrical",
        "equipment": discipline_key in ("mechanical", "electrical", "plumbing"),
        "other": True,  # fallback — let the agent see it; harmless
    }.get(schedule_type, False)


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


async def run_discipline_agent(
    project_id: str,
    discipline: Discipline,
    *,
    yolo_symbols: list[dict] | None = None,
    symbol_legend_summary: str = "",
) -> DisciplineAgentResult:
    """Run one discipline's 4-step agent end-to-end."""
    client = _get_client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    async with SessionLocal() as db:
        corpus = await gather_corpus_for_discipline(db, project_id, discipline)

    if yolo_symbols:
        corpus.yolo_symbols = yolo_symbols
    if symbol_legend_summary:
        corpus.symbol_legend_summary = symbol_legend_summary

    if not corpus.spec_chunks and not corpus.drawing_chunks:
        log.info(
            "discipline_agent: %s — no corpus available, skipping",
            discipline.key,
        )
        return DisciplineAgentResult(
            discipline=discipline.key,
            mandates_count=0,
            work_items_count=0,
            spec_without_drawing=[],
            drawing_without_spec=[],
            scope_items=[],
            cost_usd=0.0,
            latency_ms=0,
            raw={},
        )

    # Build the cached prompt: discipline corpus first (cached), task
    # framing last (uncached so different downstream calls reuse the
    # same cached corpus).
    prompt = (
        f"=== PROJECT PROFILE ===\n{corpus.profile_summary}\n\n"
        f"=== SHEET MANIFEST ({len(corpus.sheet_manifest)} sheets in {discipline.label}) ===\n"
        + "\n".join(
            f"  {s['sheet_id']:>8} - {s.get('title') or '?'}"
            for s in corpus.sheet_manifest
        )
        + (
            "\n\n=== PROJECT SYMBOL LEGEND ===\n" + corpus.symbol_legend_summary
            if corpus.symbol_legend_summary
            else ""
        )
        + (
            "\n\n=== YOLO MEP SYMBOLS DETECTED ===\n"
            + "\n".join(
                f"  {s.get('symbol_type')}: {s.get('count')} occurrences"
                for s in corpus.yolo_symbols
            )
            if corpus.yolo_symbols
            else ""
        )
        + f"\n\n=== TYPED SCHEDULE ROWS ({len(corpus.typed_schedule_rows)} total) ===\n"
        + _format_typed_schedule_block(corpus.typed_schedule_rows[:200])
        + f"\n\n=== SPEC CHUNKS ({len(corpus.spec_chunks)} for divisions {','.join(discipline.csi_divisions)}) ===\n"
        + _format_chunk_block(corpus.spec_chunks[:60], "SPEC")
        + f"\n\n=== DRAWING CHUNKS ({len(corpus.drawing_chunks)} from {discipline.label} sheets) ===\n"
        + _format_chunk_block(corpus.drawing_chunks[:80], "DRAWING")
        + "\n\n=== TASK ===\nRun the 4-step protocol now and call emit_discipline_scope."
    )

    t0 = time.perf_counter()
    msg = await client.messages.create(
        model=settings.vision_model,  # Sonnet 4.6
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT_TEMPLATE.format(discipline_label=discipline.label),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[_AGENT_TOOL],
        tool_choice={"type": "tool", "name": "emit_discipline_scope"},
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "emit_discipline_scope"
        ):
            payload = block.input
            break

    cost: float = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose=f"discipline-{discipline.key}",
            model=settings.vision_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
        )
        cost = c or 0.0
        await db.commit()

    log.info(
        "discipline_agent: %s — %d mandates, %d work_items, %d scope_items, "
        "%d gaps (%d spec_only + %d drawing_only) in %dms ($%.4f)",
        discipline.key,
        len(payload.get("mandates") or []),
        len(payload.get("work_items") or []),
        len(payload.get("scope_items") or []),
        (
            len(payload.get("spec_without_drawing") or [])
            + len(payload.get("drawing_without_spec") or [])
        ),
        len(payload.get("spec_without_drawing") or []),
        len(payload.get("drawing_without_spec") or []),
        latency_ms,
        cost,
    )

    return DisciplineAgentResult(
        discipline=discipline.key,
        mandates_count=len(payload.get("mandates") or []),
        work_items_count=len(payload.get("work_items") or []),
        spec_without_drawing=payload.get("spec_without_drawing") or [],
        drawing_without_spec=payload.get("drawing_without_spec") or [],
        scope_items=payload.get("scope_items") or [],
        cost_usd=cost,
        latency_ms=latency_ms,
        raw=payload,
    )
