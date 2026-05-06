"""P3 — project-specific symbol legend extractor (W3 mitigation).

Per the design doc, "highest leverage" mitigation for W3 (drawing
symbology misreading). Every drawing set has a Symbols & Abbreviations
sheet — usually G-001 / G-002 in the general sheets, sometimes embedded
on individual discipline cover sheets (M-001, E-001, FP-001).

Approach:
  1. Identify candidate legend sheets via SheetIndex (any sheet with
     "SYMBOL" / "LEGEND" / "ABBREVIATION" in the title, OR any G-series
     sheet, OR any "*-001" discipline cover sheet).
  2. For each candidate, run Sonnet vision with a structured tool that
     returns [{symbol_glyph, meaning, csi_section_hint}] tuples.
  3. Persist as SymbolLegend rows tagged with the source sheet's
     discipline (so the FP agent only sees FP symbols, etc.).

Output is consumed by:
  - discipline_agent: included in the cached corpus so the agent reads
    drawings with project-specific symbol→meaning context
  - YOLO MEP pre-pass (when shipped): legend entries become few-shot
    reference labels for the ViT classifier per crop
  - HITL UI: reviewer can see what each symbol means without scrolling
    back to the legend sheet

Cost: ~$0.10-0.20 per project (1-3 candidate legend sheets × Sonnet
vision call each). Idempotent: re-running wipes existing
SymbolLegend rows for the project before inserting fresh ones.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, select

from ..config import settings
from ..database import SessionLocal
from ..models import (
    Document,
    DocumentPage,
    PageExtraction,
    SheetIndex,
    SymbolLegend,
)
from .discipline_config import discipline_for_sheet_prefix
from .llm_log import record_call, usage_from_anthropic
from .renderer import read_image_for_vision, render_page_at_dpi
from .storage import storage

log = logging.getLogger(__name__)


_LEGEND_TITLE_HINTS = ("SYMBOL", "LEGEND", "ABBREVIATION", "ABBR")


def _normalize_sheet_id(sid: str | None) -> str:
    """Strip dashes / dots / spaces — 'G-001' / 'G.001' / 'G 001' → 'G001'."""
    if not sid:
        return ""
    return "".join(c for c in sid.upper() if c.isalnum())


def _sheet_id_variants(sid: str | None) -> set[str]:
    """All plausible printed forms of one sheet id.

    Drawing-set authors are wildly inconsistent: 'G01', 'G-01', 'G001',
    'G0.1' all refer to the same sheet. The vision pre-pass writes
    PageExtraction.sheet_number using whatever it read off the page,
    while the cover-sheet's Drawing Index (which feeds SheetIndex)
    uses whatever the architect typed there. Generate every form so
    we can match either side.
    """
    if not sid:
        return set()
    s = sid.upper()
    norm = _normalize_sheet_id(s)
    out: set[str] = {s, norm}
    # Split alpha prefix and numeric suffix
    i = 0
    while i < len(norm) and norm[i].isalpha():
        i += 1
    prefix, digits = norm[:i], norm[i:]
    if prefix and digits:
        # G001 / G01 / G1 — try zero-stripped & zero-padded forms
        stripped = digits.lstrip("0") or "0"
        out.add(f"{prefix}{stripped}")
        out.add(f"{prefix}{stripped.zfill(2)}")
        out.add(f"{prefix}{stripped.zfill(3)}")
        # G0.1 / G0-1 — common dotted/dashed forms
        if len(digits) >= 2:
            out.add(f"{prefix}{digits[0]}.{digits[1:]}")
            out.add(f"{prefix}{digits[0]}-{digits[1:]}")
    return {v for v in out if v}


_EXTRACT_TOOL = {
    "name": "extract_symbol_legend",
    "description": (
        "Extract every (symbol, meaning) pair from this Symbols & "
        "Abbreviations sheet. Symbols may be letters ('CW'), short "
        "codes ('PG'), graphical glyphs (drawn icons), or detail "
        "callout markers ('A1' inside a circle). Capture each one with "
        "its meaning verbatim from the legend. If the legend hints at "
        "a CSI section (often via a header like 'PLUMBING SYMBOLS' "
        "covering Div 22), tag csi_section_hint."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol_glyph": {
                            "type": "string",
                            "description": (
                                "The symbol as printed/drawn — short text "
                                "code or short verbal description of the "
                                "graphical glyph (e.g. 'PG', '▽', "
                                "'rectangle with X', 'A1 in circle')"
                            ),
                        },
                        "meaning": {
                            "type": "string",
                            "description": "Full meaning as printed in the legend",
                        },
                        "discipline_hint": {
                            "type": ["string", "null"],
                            "description": (
                                "If this symbol is grouped under a discipline "
                                "header (e.g. 'PLUMBING SYMBOLS'), the "
                                "discipline. Else null."
                            ),
                            "enum": [
                                "architectural", "structural", "mechanical",
                                "electrical", "plumbing", "fire-protection",
                                "civil", "interior", "general", None,
                            ],
                        },
                        "csi_section_hint": {
                            "type": ["string", "null"],
                            "description": (
                                "Best-guess CSI 6-digit section the symbol "
                                "implies (e.g. toilet symbol → '22 40 00')"
                            ),
                        },
                    },
                    "required": ["symbol_glyph", "meaning"],
                },
            },
        },
        "required": ["entries"],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT = """\
You are reading a SYMBOLS & ABBREVIATIONS legend sheet from a \
construction drawing set. Extract EVERY symbol-to-meaning pair.

A legend sheet is structured as a key/value list:
    PG  =  Pre-Action Sprinkler Riser
    OS&Y  =  Outside Stem & Yoke (sprinkler valve)
    [▽ inside square]  =  Floor Drain
    [A1 inside circle]  =  Detail callout, see /A1.1

Rules:
  - Capture the symbol VERBATIM — short codes as-is, glyphs described
    in 3-5 words ("rectangle with X", "diamond with letter D")
  - Capture the meaning VERBATIM — don't expand or normalize
  - If symbols are grouped under a header (e.g. "PLUMBING SYMBOLS",
    "ELECTRICAL ABBREVIATIONS"), tag discipline_hint accordingly
  - Best-guess csi_section_hint where one is obvious from the meaning
    (e.g. "Floor Drain" → "22 14 00", "Light Fixture" → "26 51 00")

Be exhaustive — missing symbols hurts downstream extraction accuracy.

Use extract_symbol_legend now.
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


@dataclass
class _LegendCandidate:
    """A page worth running through the legend extractor.

    May originate from SheetIndex (catalogued in the cover-sheet
    Drawing Index) or directly from PageExtraction (rendered page that
    looks like a legend by sheet_number/title prefix). Carries enough
    metadata for `_extract_one_legend` to render the right page.
    """

    project_id: str
    document_id: str
    sheet_id: str
    title: str | None
    discipline: str | None


async def _identify_legend_sheets(project_id: str) -> list[_LegendCandidate]:
    """Find candidate Symbols & Abbreviations sheets in this project.

    Two parallel discovery paths:
      A. SheetIndex (cover-sheet Drawing Index) — title hints,
         G-series, *-001/*-002 discipline covers.
      B. PageExtraction (rendered pages) — same hints applied to
         per-page sheet_number/sheet_title. Catches legend sheets
         that didn't make it into the Drawing Index, which is common
         on smaller projects where the index isn't exhaustive.
    """
    async with SessionLocal() as db:
        sheets = (
            await db.execute(
                select(SheetIndex)
                .where(SheetIndex.project_id == project_id)
            )
        ).scalars().all()
        pe_rows = (
            await db.execute(
                select(PageExtraction, Document)
                .join(Document, PageExtraction.document_id == Document.id)
                .where(Document.project_id == project_id)
                .where(Document.doc_type == "drawing-set")
            )
        ).all()

    seen_keys: set[tuple[str, str]] = set()
    candidates: list[_LegendCandidate] = []

    def _add(c: _LegendCandidate):
        key = (c.document_id, _normalize_sheet_id(c.sheet_id))
        if not key[1] or key in seen_keys:
            return
        seen_keys.add(key)
        candidates.append(c)

    def _is_cover_or_general(sid: str | None) -> bool:
        norm = _normalize_sheet_id(sid)
        if not norm:
            return False
        if norm.startswith("G"):
            return True
        if norm[0].isalpha():
            digits = norm[1:]
            if digits.lstrip("0") in ("", "1", "2"):  # *-001/*-002/*-01/*-1
                return True
        return False

    def _title_hits(title: str | None) -> bool:
        t = (title or "").upper()
        return any(h in t for h in _LEGEND_TITLE_HINTS)

    # Path A — SheetIndex
    for s in sheets:
        if _title_hits(s.title) or _is_cover_or_general(s.sheet_id):
            _add(
                _LegendCandidate(
                    project_id=s.project_id,
                    document_id=s.document_id,
                    sheet_id=s.sheet_id,
                    title=s.title,
                    discipline=s.discipline,
                )
            )

    # Path B — PageExtraction (rendered pages)
    for pe, doc in pe_rows:
        if _title_hits(pe.sheet_title) or _is_cover_or_general(pe.sheet_number):
            _add(
                _LegendCandidate(
                    project_id=doc.project_id,
                    document_id=doc.id,
                    sheet_id=pe.sheet_number or "",
                    title=pe.sheet_title,
                    discipline=pe.discipline,
                )
            )

    return candidates


async def _resolve_page_extraction(
    candidate: _LegendCandidate,
) -> tuple[Document, PageExtraction] | None:
    """Find the Document + PageExtraction this candidate maps to.

    Tries every plausible printed form of the sheet_id (G01 / G-01 /
    G001 / G0.1 / etc.) — Drawing Index entries and per-page
    sheet_number values are inconsistent across projects.
    """
    variants = _sheet_id_variants(candidate.sheet_id)
    if not variants:
        return None

    async with SessionLocal() as db:
        doc = await db.get(Document, candidate.document_id)
        if doc is None:
            return None
        pe = (
            await db.execute(
                select(PageExtraction)
                .where(PageExtraction.document_id == candidate.document_id)
                .where(PageExtraction.sheet_number.in_(variants))
                .order_by(PageExtraction.page_number)
                .limit(1)
            )
        ).scalar_one_or_none()
        if pe is None:
            # Don't fall back to page 1 — that silently extracts the
            # cover sheet's symbols against the wrong sheet_id, which
            # then poisons the discipline tagging downstream. Skip.
            log.info(
                "symbol_legend: no PageExtraction for sheet_id=%s "
                "(tried %s) in document=%s — skipping",
                candidate.sheet_id, sorted(variants), candidate.document_id,
            )
            return None
        return doc, pe


async def _extract_one_legend(
    candidate: _LegendCandidate,
    *,
    dpi: int = 300,
) -> list[SymbolLegend]:
    """Run Sonnet vision against one candidate legend sheet."""
    client = _get_client()
    if client is None:
        return []

    resolved = await _resolve_page_extraction(candidate)
    if resolved is None:
        return []
    doc, pe = resolved

    source_path = storage.absolute_path(doc.storage_path)
    image_path = await render_page_at_dpi(
        source_path, doc.id, pe.page_number, dpi=dpi
    )
    raw, media_type = read_image_for_vision(Path(image_path))
    b64 = base64.standard_b64encode(raw).decode("ascii")

    t0 = time.perf_counter()
    try:
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
            tool_choice={"type": "tool", "name": "extract_symbol_legend"},
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
                                f"Extract every symbol from sheet "
                                f"{candidate.sheet_id} "
                                f"({candidate.title or 'untitled'})."
                            ),
                        },
                    ],
                }
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "symbol_legend: API error on sheet %s: %s",
            candidate.sheet_id, e,
        )
        return []
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "extract_symbol_legend"
        ):
            payload = block.input
            break

    legend_rows: list[SymbolLegend] = []
    sheet_default_discipline = (
        candidate.discipline or discipline_for_sheet_prefix(candidate.sheet_id)
    )
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        glyph = (entry.get("symbol_glyph") or "").strip()
        meaning = (entry.get("meaning") or "").strip()
        if not glyph or not meaning:
            continue
        legend_rows.append(
            SymbolLegend(
                project_id=candidate.project_id,
                document_id=candidate.document_id,
                page_extraction_id=pe.id,
                symbol_glyph=glyph,
                meaning=meaning,
                discipline=(
                    entry.get("discipline_hint") or sheet_default_discipline
                ),
                source_sheet_id=candidate.sheet_id,
                csi_section_hint=entry.get("csi_section_hint") or None,
            )
        )

    cost = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="symbol-legend",
            model=settings.vision_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=candidate.project_id,
            document_id=candidate.document_id,
            page_extraction_id=pe.id,
        )
        cost = c or 0.0
        await db.commit()

    log.info(
        "symbol_legend: %s — %d entries in %dms ($%.4f)",
        candidate.sheet_id, len(legend_rows), latency_ms, cost,
    )
    return legend_rows


async def extract_for_project(project_id: str) -> dict:
    """Find candidate legend sheets + extract every symbol entry.

    Returns {"sheets_processed", "entries", "cost_usd"}.
    """
    candidates = await _identify_legend_sheets(project_id)
    if not candidates:
        log.info(
            "symbol_legend: no candidate legend sheets found for %s "
            "(no SheetIndex rows? no G-series sheets?)",
            project_id,
        )
        return {"sheets_processed": 0, "entries": 0, "cost_usd": 0.0}

    log.info(
        "symbol_legend: %d candidate sheets for %s",
        len(candidates), project_id,
    )

    sem = asyncio.Semaphore(3)

    async def with_sem(c: _LegendCandidate):
        async with sem:
            return await _extract_one_legend(c)

    results = await asyncio.gather(*(with_sem(c) for c in candidates))

    # P4 — backfill csi_section_hint from the canonical industry table
    # for any glyph the vision pass didn't tag. Project-specific
    # overrides remain authoritative; this only fills empties.
    from .symbol_to_csi import backfill_legend_hints

    backfilled = 0
    for rows in results:
        backfilled += backfill_legend_hints(rows)
    if backfilled:
        log.info(
            "symbol_legend: backfilled %d csi_section_hint(s) from "
            "canonical symbol_to_csi table", backfilled,
        )

    # Wipe + insert (idempotent)
    async with SessionLocal() as db:
        await db.execute(
            SymbolLegend.__table__.delete().where(
                SymbolLegend.project_id == project_id
            )
        )
        total_entries = 0
        for rows in results:
            for r in rows:
                db.add(r)
                total_entries += 1
        await db.commit()

    # Cost is logged via record_call inside _extract_one_legend; sum
    # via llm_calls
    from sqlalchemy import func
    from ..models import LLMCall

    async with SessionLocal() as db:
        cost_row = (
            await db.execute(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                .where(LLMCall.project_id == project_id)
                .where(LLMCall.purpose == "symbol-legend")
            )
        ).scalar()

    return {
        "sheets_processed": len(candidates),
        "entries": total_entries,
        "cost_usd": float(cost_row or 0.0),
    }


# -----------------------------------------------------------------------------
# Read helpers consumed by the discipline agent
# -----------------------------------------------------------------------------


async def get_legend_summary(project_id: str, *, discipline: str | None = None) -> str:
    """Render the legend as a plain-text block for inclusion in agent prompts."""
    async with SessionLocal() as db:
        q = select(SymbolLegend).where(SymbolLegend.project_id == project_id)
        if discipline:
            q = q.where(SymbolLegend.discipline == discipline)
        rows = (await db.execute(q.order_by(SymbolLegend.symbol_glyph))).scalars().all()
    if not rows:
        return ""
    parts: list[str] = []
    for r in rows:
        line = f"  {r.symbol_glyph:<10} = {r.meaning}"
        if r.csi_section_hint:
            line += f"  [csi: {r.csi_section_hint}]"
        parts.append(line)
    return "\n".join(parts)
