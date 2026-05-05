"""Phase 4.1 — Project Profiler.

One Claude Sonnet 4.6 call that reads the project's cover sheet and general
notes, returning structured project metadata. The profile feeds:
  - Phase 4.2 (relevance filter) — "is this trade applicable here?"
  - Phase 4.3 (scope extractor) — context grounding for every trade query
  - UI — operator can sanity-check the project at a glance

Inputs are pulled from the Phase-3 index, not by re-calling vision. We ask
the retriever for chunks tagged as cover/general-notes content (sheet
numbers like CVR, A0.0, A0.1, C001, S0.1, M0.1, P0.1, FP0.1, E0.1) plus a
broad "project description, building size, occupancy, codes" search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Chunk, ProjectProfile
from .llm_log import Usage, record_call, usage_from_anthropic
from .retriever import hybrid_retrieve

log = logging.getLogger(__name__)


class ProfilerUnavailable(Exception):
    """Raised when ANTHROPIC_API_KEY is not configured."""


_PROFILE_TOOL = {
    "name": "extract_project_profile",
    "description": (
        "Extract canonical project metadata from the supplied chunks "
        "(cover sheet, general notes, life-safety plan). Be conservative: "
        "if a value isn't clearly stated, return null rather than guessing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "building_type": {
                "type": ["string", "null"],
                "description": (
                    "Short noun phrase, e.g. 'community enrichment center', "
                    "'school addition', 'tenant fit-out'. Lowercase."
                ),
            },
            "size_sf": {
                "type": ["number", "null"],
                "description": "Floor area in square feet (number only).",
            },
            "occupancy": {
                "type": ["string", "null"],
                "description": (
                    "IBC occupancy classification(s), e.g. 'A-2/B', 'R-2', 'E'."
                ),
            },
            "construction_type": {
                "type": ["string", "null"],
                "description": "IBC construction type, e.g. 'Type V-B', 'Type II-A'.",
            },
            "sprinklered": {
                "type": ["boolean", "null"],
                "description": "True if explicitly stated as sprinklered.",
            },
            "stories": {
                "type": ["integer", "null"],
                "description": "Actual story count above grade.",
            },
            "location": {
                "type": ["string", "null"],
                "description": "City and state, e.g. 'Oak Ridge, TN'.",
            },
            "project_number": {
                "type": ["string", "null"],
                "description": "Architect/firm project number, e.g. '25026'.",
            },
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Cited code editions only — e.g. ['IBC 2024', 'NFPA 13 2019', "
                    "'IECC 2018']. Empty list if no codes are stated."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence summarising what evidence drove the answers.",
            },
        },
        "required": ["reasoning", "codes"],
    },
}


# Sheet numbers that typically carry project-level info. We boost retrieval
# of chunks from these sheets but still issue a semantic search to catch
# anything else that mentions the project context.
_PROFILE_SHEET_HINTS = {
    "CVR", "G0.1", "G1.1",          # cover / general info
    "A0.0", "A0.1",                  # arch general notes / life safety
    "S0.1",                          # structural notes
    "M0.1", "P0.1", "FP0.1",         # MEP general notes
    "E0.1", "E0.2",                  # electrical general notes
    "C001", "C100",                  # civil cover / general notes
}


_PROFILE_QUERY = (
    "project description building type size occupancy classification "
    "construction type sprinklered stories location code IBC NFPA"
)


@dataclass
class ProfileResult:
    profile: dict  # parsed tool input (matches PROFILE_TOOL schema)
    usage: Usage
    latency_ms: int
    model: str


def _get_client():
    if not settings.anthropic_api_key:
        raise ProfilerUnavailable("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _gather_profile_chunks(db: AsyncSession, project_id: str) -> list[Chunk]:
    """Pick chunks that most likely mention project-level info.

    Three pools, dedup'd:
      A) Cover-sheet pool — every chunk on page 1 of any document. The cover
         usually contains a structured schedule with the IBC fields we want.
      B) Hint-sheet pool — every chunk on general-notes / life-safety sheets
         (A0.0/A0.1/S0.1/M0.1/etc.).
      C) Semantic pool — top-K chunks from a targeted query about project
         description, codes, occupancy, etc.

    All three matter because the cover-sheet schedule (e.g. PROJECT
    INFORMATION) is usually the canonical source — but the schedule chunk
    type isn't always semantically retrievable, and hint sheets often have
    secondary context.
    """
    # A) Cover pool — ALL chunk types on page 1 of any doc.
    cover_q = await db.execute(
        select(Chunk)
        .where(Chunk.project_id == project_id)
        .where(Chunk.page_number == 1)
    )
    cover_chunks = list(cover_q.scalars().all())

    # B) Hint-sheet pool — Python-side filter on JSON metadata.
    all_chunks_q = await db.execute(
        select(Chunk).where(Chunk.project_id == project_id)
    )
    sheet_chunks = [
        c
        for c in all_chunks_q.scalars().all()
        if (c.extra or {}).get("sheet_number") in _PROFILE_SHEET_HINTS
    ]

    # C) Semantic pool — broad project-context query.
    retrieved = await hybrid_retrieve(db, project_id, _PROFILE_QUERY, top_k=40)
    semantic = [r.chunk for r in retrieved]

    # De-duplicate while preserving cover-first priority.
    seen: set[str] = set()
    out: list[Chunk] = []
    for c in (*cover_chunks, *sheet_chunks, *semantic):
        if c.id in seen:
            continue
        seen.add(c.id)
        out.append(c)
    # Cap context size — Sonnet 4.6 has 200K but we don't need much.
    return out[:120]


def _format_chunks_for_prompt(chunks: list[Chunk]) -> str:
    parts: list[str] = []
    for c in chunks:
        meta = c.extra or {}
        sheet = meta.get("sheet_number")
        title = meta.get("sheet_title")
        header = f"[{c.chunk_type}"
        if sheet:
            header += f" · sheet {sheet}"
        if title:
            header += f" · {title}"
        if c.page_number:
            header += f" · page {c.page_number}"
        header += "]"
        parts.append(f"{header}\n{c.text}\n")
    return "\n---\n".join(parts)


async def profile_project(project_id: str) -> ProfileResult:
    """Run the profiler on a project; return the result. Does NOT persist."""
    from ..database import SessionLocal

    async with SessionLocal() as db:
        chunks = await _gather_profile_chunks(db, project_id)

    if not chunks:
        raise RuntimeError(
            "no indexed chunks found — ensure project documents finished "
            "Phase 1-3 processing before profiling"
        )

    log.info(
        "profiler: project %s — %d chunks gathered for profile prompt",
        project_id,
        len(chunks),
    )

    client = _get_client()
    prompt_chunks = _format_chunks_for_prompt(chunks)

    t0 = time.perf_counter()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        tools=[_PROFILE_TOOL],
        tool_choice={"type": "tool", "name": "extract_project_profile"},
        messages=[
            {
                "role": "user",
                "content": (
                    "You are profiling a construction project from its general-notes "
                    "and cover-sheet text. Use the chunks below to fill the schema. "
                    "Return null for anything not clearly stated.\n\n"
                    f"---\n{prompt_chunks}\n---\n\n"
                    "Use the extract_project_profile tool now."
                ),
            }
        ],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    profile_data: dict | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_project_profile":
            profile_data = block.input
            break

    if profile_data is None:
        raise RuntimeError("profiler: no tool_use block in Claude response")

    return ProfileResult(
        profile=profile_data,
        usage=usage_from_anthropic(response),
        latency_ms=latency_ms,
        model="claude-sonnet-4-6",
    )


async def upsert_project_profile(project_id: str) -> ProjectProfile:
    """Run profiler and persist the result. Replaces any existing row."""
    from ..database import SessionLocal

    result = await profile_project(project_id)
    p = result.profile

    async with SessionLocal() as db:
        existing = await db.execute(
            select(ProjectProfile).where(ProjectProfile.project_id == project_id)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            row = ProjectProfile(project_id=project_id)
            db.add(row)

        row.building_type = p.get("building_type")
        row.size_sf = p.get("size_sf")
        row.occupancy = p.get("occupancy")
        row.construction_type = p.get("construction_type")
        row.sprinklered = p.get("sprinklered")
        row.stories = p.get("stories")
        row.location = p.get("location")
        row.project_number = p.get("project_number")
        row.codes = p.get("codes") or []
        row.reasoning = p.get("reasoning")

        row.raw_response = json.loads(json.dumps(p))  # ensure JSON-clean
        row.model = result.model
        row.latency_ms = result.latency_ms

        cost, _ = await record_call(
            db,
            purpose="profile",
            model=result.model,
            usage=result.usage,
            latency_ms=result.latency_ms,
            project_id=project_id,
        )
        row.cost_usd = cost

        await db.commit()
        await db.refresh(row)

    log.info(
        "profiler: %s saved — type=%s size=%s occupancy=%s cost=$%.4f",
        project_id,
        row.building_type,
        row.size_sf,
        row.occupancy,
        row.cost_usd or 0.0,
    )
    return row


async def get_or_create_profile(project_id: str) -> ProjectProfile:
    """Return existing profile, or run + persist if none exists."""
    from ..database import SessionLocal

    async with SessionLocal() as db:
        result = await db.execute(
            select(ProjectProfile).where(ProjectProfile.project_id == project_id)
        )
        row = result.scalar_one_or_none()
    if row is not None:
        return row
    return await upsert_project_profile(project_id)
