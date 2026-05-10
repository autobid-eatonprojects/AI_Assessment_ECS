"""Section-scoped scope extractor — replaces the discipline_agent's
text-extraction pass with one Sonnet call per CSI section.

Why per-section instead of per-discipline:
  - The spec book IS organized by section. The natural extraction unit is
    one section's spec text, not a 100-page discipline corpus.
  - Each call's prompt is small and focused — fewer tokens, faster, cheaper,
    less prone to dropping items in the middle of a long context.
  - Items emit with `item_type` (material / equipment / admin / demo) +
    `expected_pattern` (bilateral / spec_only / drawing_only). No bilateral
    mandate at extraction time — items can be one-sided when that's correct
    (Division 1 Submittals legitimately has no drawing).
  - Drawing evidence is added later by `drawing_grounder.py` (vision pass).

Output: one or more CandidateItem per section. The caller persists them
through the same scope_runner persistence path (citations, link judge,
trust score, evidence_pattern rollup all unchanged).

Run path inside scope_runner is: section_extractor → drawing_grounder →
schedule_miner → dedupe → persist → link_judge → bilateral_evidence rollup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Chunk, Document
from .citation_validator import validate_citation
from .evidence_pattern import EvidencePattern, expected_pattern
from .llm_log import Usage, record_call, usage_from_anthropic
from .retriever import RetrievedChunk
from .scope_extractor import CandidateItem
from .trade_list_parser import CSISection, CSITaxonomy

log = logging.getLogger(__name__)


_SECTION_CONCURRENCY = 8
# Cap to keep prompts bounded. Most sections fit in <30 chunks; dense
# sections like `08 71 00 Door Hardware` can run longer. Bumped from 30
# to 50 to cover those without losing tail content; cost increase is
# marginal because most sections never hit the cap.
_MAX_CHUNKS_PER_PROMPT = 50
_MAX_CHUNK_TEXT_CHARS = 1500

# Inline-citation L2 threshold — tighter than the default 0.10 used at
# persistence time. Within a single CSI section, all chunks share section
# boilerplate (ANSI codes, manufacturer names), so 0.10 lets through too
# many "right section, wrong sub-section" cites. 0.18 demands the chunk
# actually share specific item tokens, not just generic section terms.
_INLINE_CITE_MIN_OVERLAP = 0.18


_EXTRACT_TOOL = {
    "name": "emit_section_items",
    "description": (
        "Emit every biddable scope item defined in this CSI section. "
        "Each item gets an item_type so the downstream pipeline knows "
        "whether to expect drawing evidence (material/equipment) or not "
        "(admin/demo). Cite the spec_chunk_ids you actually read."
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
                            "description": "6-digit '03 30 00' format. Use the section being extracted unless this item belongs in a sub-section.",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "One concise sentence naming the bid item. MUST start "
                                "with an industry-standard action verb so the line "
                                "reads like a real bid scope: 'Furnish and install...' "
                                "(material), 'Furnish, install, and connect...' "
                                "(equipment), 'Provide...' (admin), 'Remove and dispose...' "
                                "(demo), 'Test and certify...' (qc). Then describe the "
                                "specific scope using the spec wording."
                            ),
                        },
                        "specification": {
                            "type": ["string", "null"],
                            "description": "Brief spec excerpt — material/standard/performance class.",
                        },
                        "quantity": {
                            "type": ["string", "null"],
                            "description": "Use '1' as a placeholder for material codes (qty comes from drawing takeoff). Use the actual count when the spec gives one.",
                        },
                        "unit": {
                            "type": ["string", "null"],
                            "description": "EA / SF / LF / CY / TON / etc.",
                        },
                        "item_type": {
                            "type": "string",
                            "enum": ["material", "equipment", "admin", "demo", "qc"],
                            "description": (
                                "material = installed material (concrete, drywall, paint). "
                                "equipment = tagged equipment unit (AHU, panel, fixture). "
                                "admin = administrative scope (submittals, mockups, warranties, mobilization, closeout). "
                                "demo = demolition extent (typically drawing-only). "
                                "qc = quality control / testing requirements (typically spec-only)."
                            ),
                        },
                        "spec_chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "Chunk IDs (from the chunk blocks shown) that establish this item.",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": [
                        "csi_code", "description", "item_type",
                        "spec_chunk_ids", "confidence",
                    ],
                },
            }
        },
        "required": ["items"],
    },
    "cache_control": {"type": "ephemeral"},
}


@dataclass
class SectionExtractionResult:
    section: CSISection
    items: list[CandidateItem]
    cost_usd: float = 0.0
    latency_ms: int = 0
    chunks_used: int = 0
    notes: list[str] = field(default_factory=list)


def _get_client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _format_chunk_block(chunks: list[Chunk]) -> str:
    """Render the section's chunks as labeled blocks the model can cite."""
    parts: list[str] = []
    for c in chunks:
        text = (c.text or "").strip()
        if len(text) > _MAX_CHUNK_TEXT_CHARS:
            text = text[:_MAX_CHUNK_TEXT_CHARS] + "…"
        parts.append(f"[chunk_id={c.id} page={c.page_number}]\n{text}")
    return "\n\n---\n\n".join(parts)


def _build_prompt(section: CSISection, chunks: list[Chunk]) -> str:
    body = _format_chunk_block(chunks)
    return (
        f"You are extracting bid scope from one CSI section of a construction "
        f"spec book. Read every chunk shown, then emit one item per biddable "
        f"unit of work — be exhaustive.\n\n"
        f"=== SECTION ===\n"
        f"Code:  {section.code}\n"
        f"Title: {section.title}\n\n"
        f"=== SPEC CHUNKS ({len(chunks)}) ===\n"
        f"{body}\n\n"
        f"=== TASK ===\n"
        f"Use emit_section_items. For EACH biddable element this section "
        f"defines, emit one item. Classify item_type carefully:\n\n"
        f"  - material: installed material (concrete mix, drywall type, paint product, "
        f"insulation, sealant, asphalt, masonry unit, etc.)\n"
        f"  - equipment: tagged equipment (AHU-1, WC-1, light fixtures, panels, doors)\n"
        f"  - admin: administrative scope only — submittal procedures, mockups, "
        f"warranties, mobilization, project closeout, schedule of values, bonds, permits. "
        f"These items are SPEC-ONLY by nature; they are not drawn. Don't avoid emitting "
        f"them just because they're admin — they are real bid scope.\n"
        f"  - demo: demolition extent shown on drawings, called out in spec text\n"
        f"  - qc: quality control / testing requirements (mockup tests, field testing)\n\n"
        f"Quantity rule: for materials use quantity='1' as a placeholder unless the "
        f"spec gives an explicit count — actual quantities come from drawing takeoff. "
        f"For equipment with a count in the spec, use that count.\n\n"
        f"DESCRIPTION FORMAT — start with an industry-standard action verb so the "
        f"line reads as a real bid scope an estimator could send to a sub:\n"
        f"  - material  → 'Furnish and install ...'\n"
        f"  - equipment → 'Furnish, install, and connect ...'\n"
        f"  - admin     → 'Provide ...' (e.g. 'Provide samples for', 'Provide submittals')\n"
        f"  - demo      → 'Remove and dispose of ...'\n"
        f"  - qc        → 'Test and certify ...' or 'Provide quality control for ...'\n"
        f"Examples:\n"
        f"  ✓ 'Furnish and install 5/8\" Type X gypsum board on 3-5/8\" 25 ga metal studs'\n"
        f"  ✓ 'Provide submittals for grout, including manufacturer's product data'\n"
        f"  ✓ 'Remove and dispose of existing concrete sidewalk'\n"
        f"  ✗ 'Gypsum board' (no action verb)\n"
        f"  ✗ 'Submittals for grout' (no action verb)\n\n"
        f"CITATION RULE — strict. For each item, cite ONLY the specific chunk_ids "
        f"whose text literally describes that item. Do not cite other chunks from "
        f"the same section just because they're related to the section. If a 'Samples' "
        f"submittal item is described in chunk X, cite chunk X — not the chunk about "
        f"'Keying Conference' even though both belong to this section. If you cannot "
        f"identify a specific chunk that supports the item, do NOT emit the item.\n\n"
        f"Be exhaustive — if Section 01 33 00 has 12 distinct submittal types, emit 12 "
        f"admin items, not one summary."
    )


async def _extract_one(
    client,
    project_id: str,
    section: CSISection,
    chunks: list[Chunk],
) -> SectionExtractionResult:
    if not chunks:
        return SectionExtractionResult(section=section, items=[], chunks_used=0,
                                        notes=["no chunks for section"])

    # Cap to keep the prompt bounded; sections rarely have more than 30
    # chunks, but the spec section "00 21 13 Instructions to Bidders" had
    # 40 in our project. Keep most-recent (often the body) over header/cover.
    used = chunks[:_MAX_CHUNKS_PER_PROMPT]
    if len(chunks) > _MAX_CHUNKS_PER_PROMPT:
        log.info(
            "section_extractor: %s — capping %d chunks to %d for prompt",
            section.code, len(chunks), _MAX_CHUNKS_PER_PROMPT,
        )

    prompt = _build_prompt(section, used)
    t0 = time.perf_counter()
    # 8192 max — rich sections like 08 71 00 Door Hardware can emit 40+
    # items, each ~120-150 output tokens. 4096 was clipping mid-JSON and
    # returning 0 items. 8192 gives ~50-item headroom.
    msg = await client.messages.create(
        model=settings.vision_model,
        max_tokens=8192,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "emit_section_items"},
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict | None = None
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_section_items":
            payload = block.input
            break
    if payload is None:
        log.warning("section_extractor: no tool_use for section %s", section.code)
        return SectionExtractionResult(
            section=section, items=[], chunks_used=len(used), latency_ms=latency_ms,
            notes=["model did not return emit_section_items"],
        )

    raw_items = payload.get("items") or []

    # Map chunk_id → Chunk for synthetic supporting_chunks construction
    chunks_by_id = {c.id: c for c in used}

    candidates: list[CandidateItem] = []
    cit_dropped_bad_id = 0      # chunk_id not in this section's pool
    cit_dropped_low_overlap = 0  # passed schema but failed L2 validator
    items_dropped_no_cites = 0   # all citations dropped → item dropped
    for it in raw_items:
        description = (it.get("description") or "").strip()
        spec_ids = it.get("spec_chunk_ids") or []
        candidate_chunks: list[Chunk] = []
        for cid in spec_ids:
            ch = chunks_by_id.get(cid)
            if ch is None:
                cit_dropped_bad_id += 1
                continue
            verdict = validate_citation(
                description, ch.text or "",
                min_overlap=_INLINE_CITE_MIN_OVERLAP,
            )
            if not verdict.valid:
                cit_dropped_low_overlap += 1
                log.info(
                    "section_extractor: %s — dropping cite (reason=%s overlap=%.2f) "
                    "for item=%r chunk_page=%s",
                    section.code, verdict.reason, verdict.overlap,
                    description[:60], ch.page_number,
                )
                continue
            candidate_chunks.append(ch)

        if not candidate_chunks:
            items_dropped_no_cites += 1
            log.info(
                "section_extractor: %s dropping item — all cites failed validation: %r",
                section.code, description[:80],
            )
            continue

        supporting = [
            RetrievedChunk(
                chunk=ch,
                dense_score=1.0,
                sparse_score=1.0,
                rrf_score=1.0,
                rerank_score=1.0,
                snippet=(ch.text or "")[:240],
            )
            for ch in candidate_chunks
        ]
        cand = CandidateItem(
            csi_code=(it.get("csi_code") or section.code).strip(),
            description=description,
            specification=it.get("specification"),
            quantity=str(it.get("quantity") or "1"),
            unit=it.get("unit"),
            location=None,
            extraction_method=f"section_extractor/{it.get('item_type', '?')}",
            source_chunk_ids=[ch.id for ch in candidate_chunks],
            found_by_query=f"section:{section.code}",
            supporting_chunks=supporting,
        )
        # Stash the model-claimed item_type and confidence on the candidate
        # so downstream persistence + evidence_pattern rollup can use them.
        cand._item_type = it.get("item_type")  # type: ignore[attr-defined]
        cand._confidence = float(it.get("confidence") or 0.85)  # type: ignore[attr-defined]
        candidates.append(cand)

    if cit_dropped_bad_id or cit_dropped_low_overlap or items_dropped_no_cites:
        log.info(
            "section_extractor: %s — kept %d items; dropped %d citations "
            "(bad_id=%d low_overlap=%d) and %d items with no valid cites",
            section.code, len(candidates),
            cit_dropped_bad_id + cit_dropped_low_overlap,
            cit_dropped_bad_id, cit_dropped_low_overlap,
            items_dropped_no_cites,
        )

    cost = 0.0
    from ..database import SessionLocal
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="section-extractor",
            model=settings.vision_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
        )
        cost = c or 0.0
        await db.commit()

    return SectionExtractionResult(
        section=section,
        items=candidates,
        cost_usd=cost,
        latency_ms=latency_ms,
        chunks_used=len(used),
    )


async def extract_section(
    db: AsyncSession,
    project_id: str,
    section: CSISection,
) -> SectionExtractionResult:
    """Extract every bid item defined in this spec section.

    Pulls all chunks tagged with this section's code (from the csi_section
    column populated by the indexer), prompts Sonnet to enumerate biddable
    units of work + classify each, and returns CandidateItems.

    Drawing evidence (where the items appear, counts) is NOT added here —
    that's the job of drawing_grounder.py downstream.
    """
    client = _get_client()
    if client is None:
        log.warning("section_extractor: no ANTHROPIC_API_KEY — skipping %s", section.code)
        return SectionExtractionResult(section=section, items=[],
                                        notes=["no api key"])

    chunks = (
        await db.execute(
            select(Chunk)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.project_id == project_id)
            .where(Chunk.csi_section == section.code)
            .where(Document.doc_type == "written-spec")
            .order_by(Chunk.page_number)
        )
    ).scalars().all()

    return await _extract_one(client, project_id, section, list(chunks))


async def extract_sections_for_project(
    project_id: str,
    sections: list[CSISection],
) -> tuple[list[CandidateItem], float, dict[str, int]]:
    """Run extract_section concurrently for every section in scope.

    Returns (all_candidates, total_cost_usd, stats_per_division).
    """
    client = _get_client()
    if client is None:
        return [], 0.0, {}

    from ..database import SessionLocal
    sem = asyncio.Semaphore(_SECTION_CONCURRENCY)

    async def _one(section: CSISection) -> SectionExtractionResult:
        async with sem:
            try:
                async with SessionLocal() as db:
                    return await extract_section(db, project_id, section)
            except Exception as e:  # noqa: BLE001
                log.warning("section_extractor: section %s failed: %s",
                            section.code, e)
                return SectionExtractionResult(section=section, items=[],
                                                notes=[f"error: {e}"])

    results = await asyncio.gather(*(_one(s) for s in sections))

    all_items: list[CandidateItem] = []
    total_cost = 0.0
    by_division: dict[str, int] = {}
    for r in results:
        all_items.extend(r.items)
        total_cost += r.cost_usd
        if r.items:
            div = r.section.division_code
            by_division[div] = by_division.get(div, 0) + len(r.items)
    log.info(
        "section_extractor: extracted %d items across %d sections, $%.4f",
        len(all_items), len(sections), total_cost,
    )
    return all_items, total_cost, by_division
