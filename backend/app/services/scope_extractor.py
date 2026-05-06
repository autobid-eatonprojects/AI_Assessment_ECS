"""Phase 4.3a — Multi-query EVE-pattern scope extraction per CSI division.

Per relevant division, fire **three independent extraction queries** with
different framings (materials / quantities / specs). Each runs through Phase
3 hybrid retrieval, then a Sonnet 4.6 call extracts candidate scope items.
We union the three result sets — chunks that one phrasing missed are caught
by another. This is the canonical EVE Stage A and is documented in the
research as the cleanest way to maximize recall on exhaustive enumeration.

The output is a list of `CandidateItem` per division, each with provenance:
which query found it, which chunks it cites. Phase 4.3b validates them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ProjectProfile
from .llm_log import Usage, record_call, usage_from_anthropic
from .retriever import RetrievedChunk, search as retrieve
from .trade_list_parser import CSIDivision, CSITaxonomy

log = logging.getLogger(__name__)


class ScopeExtractorUnavailable(Exception):
    """Raised when ANTHROPIC_API_KEY is missing."""


# Three independent perspectives per division. Different vocabulary —
# different matches in Phase-3 retrieval — different reasoning paths in
# Claude. Their union catches more than any single phrasing.
_QUERY_PERSPECTIVES = [
    (
        "materials",
        "What materials, products, and methods are specified for the trade "
        "{division_label}? Cover specific items the project requires.",
    ),
    (
        "quantities",
        "What quantities, dimensions, counts, and measurements does the project "
        "specify for {division_label}? Look for schedules, tables, and explicit "
        "callouts on plans.",
    ),
    (
        "specs",
        "What specifications, codes, performance criteria, and standards govern "
        "{division_label} on this project? Include any cited code editions or "
        "manufacturer / product references.",
    ),
]


_EXTRACT_TOOL = {
    "name": "extract_scope_items",
    "description": (
        "Extract every distinct, biddable scope-of-work item that this trade "
        "(CSI division) covers on this project. Each item is something a "
        "subcontractor would price separately. Be exhaustive: include schedule "
        "rows, narrative-spec callouts, and plan-mounted requirements. Don't "
        "invent items — only return things that have direct support in the "
        "supplied chunks. Use the canonical CSI 6-digit section code from the "
        "supplied list when possible."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "csi_code": {
                            "type": "string",
                            "description": (
                                "6-digit CSI section, format 'NN NN NN'. Pick from "
                                "the section list provided in the prompt; if no "
                                "section in the list applies, return the division "
                                "code as 'NN 00 00'."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "What the subcontractor will deliver — "
                                "concise noun phrase, e.g. '4-inch slab on grade "
                                "with 6x6-W2.9xW2.9 WWF'."
                            ),
                        },
                        "specification": {
                            "type": "string",
                            "description": (
                                "Material spec / performance / code references "
                                "stated in the source. Null if not stated."
                            ),
                        },
                        "quantity": {
                            "type": "string",
                            "description": (
                                "Numeric quantity as text (preserves '5,200', "
                                "'~120', etc.). Null if not stated."
                            ),
                        },
                        "unit": {
                            "type": "string",
                            "description": (
                                "Unit code: SF, LF, CY, EA, lbs, etc. Null if "
                                "not stated."
                            ),
                        },
                        "location": {
                            "type": "string",
                            "description": (
                                "Where in the building (e.g. 'level 2 corridor', "
                                "'building footprint', 'mech room'). Null if not stated."
                            ),
                        },
                        "extraction_method": {
                            "type": "string",
                            "enum": [
                                "schedule",
                                "note",
                                "spec_section",
                                "plan_callout",
                                "inferred",
                            ],
                            "description": (
                                "Where in the source the item came from. Use "
                                "'inferred' only when the item is implied but "
                                "not directly stated."
                            ),
                        },
                        "source_chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Chunk IDs from the prompt that support this "
                                "item. At least one. Use the [chunk_id=...] "
                                "tags shown in the prompt."
                            ),
                        },
                    },
                    "required": [
                        "csi_code",
                        "description",
                        "extraction_method",
                        "source_chunk_ids",
                    ],
                },
            },
        },
        "required": ["items"],
    },
}


@dataclass
class CandidateItem:
    """Output of Stage A. Becomes a ScopeItem after Stage B + C."""

    csi_code: str
    description: str
    specification: str | None
    quantity: str | None
    unit: str | None
    location: str | None
    extraction_method: str
    source_chunk_ids: list[str]
    # Provenance: which of the 3 queries found this candidate
    found_by_query: str = ""
    # Hydrated chunks (for validator)
    supporting_chunks: list[RetrievedChunk] = field(default_factory=list)


def _get_client():
    if not settings.anthropic_api_key:
        raise ScopeExtractorUnavailable("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _format_profile(profile: ProjectProfile) -> str:
    parts = ["Project context:"]
    if profile.building_type:
        parts.append(f"  - Building type: {profile.building_type}")
    if profile.size_sf:
        parts.append(f"  - Size: {profile.size_sf:,.0f} SF")
    if profile.occupancy:
        parts.append(f"  - Occupancy: {profile.occupancy}")
    if profile.construction_type:
        parts.append(f"  - Construction: {profile.construction_type}")
    if profile.sprinklered is not None:
        parts.append(f"  - Sprinklered: {profile.sprinklered}")
    if profile.codes:
        parts.append(f"  - Codes: {', '.join(profile.codes)}")
    return "\n".join(parts)


def _format_division_sections(division: CSIDivision) -> str:
    if not division.sections:
        return "(no sections listed)"
    lines = []
    for s in division.sections:
        lines.append(f"  {s.code} — {s.title}")
    return "\n".join(lines)


def _format_chunks_for_extraction(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for r in chunks:
        c = r.chunk
        meta = c.extra or {}
        sheet = meta.get("sheet_number") or "—"
        page = c.page_number or "—"
        # Truncate very long chunks
        text = c.text if len(c.text) <= 1500 else c.text[:1500] + "…"
        parts.append(
            f"[chunk_id={c.id} type={c.chunk_type} sheet={sheet} page={page}]\n{text}"
        )
    return "\n\n---\n\n".join(parts)


async def _extract_one_query(
    client,
    project_id: str,
    division: CSIDivision,
    profile: ProjectProfile,
    query_name: str,
    query_template: str,
    db: AsyncSession,
) -> tuple[list[CandidateItem], dict[str, RetrievedChunk], Usage, int]:
    """Run a single query of the EVE multi-query extractor.

    Returns: candidates + chunk_id→RetrievedChunk lookup + usage + latency_ms.
    """
    query = query_template.format(division_label=division.label)
    retrieved = await retrieve(
        db=db,
        project_id=project_id,
        query=query,
        candidates_k=50,
        final_k=20,
    )
    if not retrieved:
        return [], {}, Usage(), 0

    chunks_by_id = {r.chunk.id: r for r in retrieved}
    chunks_text = _format_chunks_for_extraction(retrieved)
    section_listing = _format_division_sections(division)
    profile_str = _format_profile(profile)

    prompt = (
        f"{profile_str}\n\n"
        f"=== TRADE: {division.label} ===\n\n"
        f"Sections in this division (use these CSI codes when applicable):\n"
        f"{section_listing}\n\n"
        f"=== EVIDENCE CHUNKS ===\n\n{chunks_text}\n\n"
        f"=== TASK ===\n"
        f"Extract every biddable scope item visible in the evidence above for "
        f"the trade {division.label}. Tag each item with the CSI section code "
        f"that best matches (from the list above). Cite the chunk_id(s) that "
        f"support each item.\n\n"
        f"Be exhaustive — schedule rows, plan callouts, notes, spec text all "
        f"count. Don't invent. If a value isn't stated, leave it null."
    )

    t0 = time.perf_counter()
    # Cache the tool schema. Each scope run makes ~3 calls per division
    # × ~14 relevant divisions = ~42 calls — cache pays for itself starting
    # at call 2.
    cached_tool = {**_EXTRACT_TOOL, "cache_control": {"type": "ephemeral"}}
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        tools=[cached_tool],
        tool_choice={"type": "tool", "name": "extract_scope_items"},
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    items: list[CandidateItem] = []
    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "extract_scope_items"
        ):
            for it in block.input.get("items", []):
                cand = CandidateItem(
                    csi_code=str(it.get("csi_code") or "").strip(),
                    description=str(it.get("description") or "").strip(),
                    specification=it.get("specification"),
                    quantity=it.get("quantity"),
                    unit=it.get("unit"),
                    location=it.get("location"),
                    extraction_method=it.get("extraction_method") or "inferred",
                    source_chunk_ids=list(it.get("source_chunk_ids") or []),
                    found_by_query=query_name,
                )
                # Hydrate supporting_chunks
                cand.supporting_chunks = [
                    chunks_by_id[cid]
                    for cid in cand.source_chunk_ids
                    if cid in chunks_by_id
                ]
                items.append(cand)
            break
    return items, chunks_by_id, usage_from_anthropic(response), latency_ms


async def extract_division_candidates(
    project_id: str,
    division: CSIDivision,
    profile: ProjectProfile,
    taxonomy: CSITaxonomy,
) -> tuple[list[CandidateItem], dict[str, RetrievedChunk], float]:
    """Run all 3 query perspectives in parallel and union the candidates.

    Returns: (unioned candidates, all_chunks_by_id, total_cost_usd).
    """
    from ..database import SessionLocal

    client = _get_client()

    async def run_query(name: str, template: str) -> tuple[list[CandidateItem], dict, Usage, int]:
        # Each query gets its own DB session (retriever needs one).
        async with SessionLocal() as db:
            return await _extract_one_query(
                client, project_id, division, profile, name, template, db
            )

    log.info(
        "scope_extractor: running 3 queries for division %s",
        division.code,
    )
    results = await asyncio.gather(
        *(run_query(name, template) for name, template in _QUERY_PERSPECTIVES),
        return_exceptions=True,
    )

    all_candidates: list[CandidateItem] = []
    all_chunks: dict[str, RetrievedChunk] = {}
    total_cost = 0.0

    async with SessionLocal() as db:
        for r in results:
            if isinstance(r, Exception):
                log.warning(
                    "scope_extractor: query failed for %s: %s", division.code, r
                )
                continue
            items, chunks_by_id, usage, latency_ms = r
            cost, _ = await record_call(
                db,
                purpose=f"scope-extract-{items[0].found_by_query if items else 'na'}",
                model="claude-sonnet-4-6",
                usage=usage,
                latency_ms=latency_ms,
                project_id=project_id,
            )
            total_cost += cost or 0.0
            all_candidates.extend(items)
            all_chunks.update(chunks_by_id)
        await db.commit()

    log.info(
        "scope_extractor: division %s — %d candidates from 3 queries, $%.4f",
        division.code,
        len(all_candidates),
        total_cost,
    )
    return all_candidates, all_chunks, total_cost
