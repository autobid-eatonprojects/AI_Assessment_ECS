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
import re
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


# =============================================================================
# Smart-miner type rules (Layer 1 of the chunker improvement plan).
#
# Per-schedule-type rules that constrain Haiku's output and inform
# post-extraction validation. Goal: cut the wrong-unit + junk-row +
# definition-vs-quantity errors that the spot-check found in the
# previous miner runs.
#
# Each entry is keyed on the schedule type tag emitted by
# schedule_extractor_typed (the [typed:X] prefix on schedule.name).
# =============================================================================
_TYPE_RULES: dict[str, dict] = {
    # Door schedule — one EA per door tag, qty from row count
    "door": {
        "csi_division_hint": "08",
        "default_unit": "EA",
        "unit_overrides": {},          # No per-content overrides — every door is EA
        "skip_predicates": ["empty_row"],
        "guidance": (
            "Door schedule — every row with a door tag is one bid item, unit=EA. "
            "Skip header rows and rows that have only a tag with no description."
        ),
    },
    # Window schedule — same pattern as doors
    "window": {
        "csi_division_hint": "08",
        "default_unit": "EA",
        "unit_overrides": {},
        "skip_predicates": ["empty_row"],
        "guidance": (
            "Window schedule — every row with a window tag is one bid item, "
            "unit=EA. Skip header rows."
        ),
    },
    # Finish schedule — the trickiest. Two sub-types in one schedule:
    #   (a) MATERIAL CODES (LVT1, FT1, B2, P1, etc.) — these are SPECS, not
    #       quantity items. The QTY comes from measuring the floor plan, not
    #       from the schedule. Treat as is_quantifiable=false unless the
    #       schedule has an actual area/length column.
    #   (b) ROOM ROWS (Room 101, Room 102) listing what code goes where —
    #       these reference back to material codes, also not bid items.
    # In both cases, the schedule itself rarely has biddable quantities.
    "finish": {
        "csi_division_hint": "09",
        "default_unit": None,           # Force model to set unit per row
        "unit_overrides": {
            # Order matters — earlier (more-specific) patterns win first.
            # Linear-foot items first so a "TR2: Schluter Resilient Edge" row
            # doesn't get caught by the RESILIENT keyword in the SF group.
            r"\b(TR[0-9]|TN[0-9]|TRANSITION|EDGE PROTECTION|SCHLUTER)\b": "LF",
            r"\b(B[0-9]|BASE TILE|BASE TRIM|WALL BASE|RUBBER BASE)\b": "LF",
            # SF items — wall tile / gyp board / ceilings / acoustic panels
            r"\b(WT\d*|WALL TILE|WP\d*|WALL PANEL)\b": "SF",
            r"\b(GWB|GB\d*|GYPSUM|DRYWALL|GYP\.|SHEETROCK)\b": "SF",
            r"\b(ACT|CEILING TILE|ACOUSTICAL CEILING|ACOUSTIC PANEL(?:ING)?|ACOUSTIC SLATS?|ACOUSTIC GRIDS?)\b": "SF",
            r"\b(WOOD SLATS?|DECK(?:ING)?|COMPOSITE DECK)\b": "SF",
            # Paint — coded P1/P2 always counts; bare "PAINT" only counts when
            # it isn't part of a door description ("paint finish/frame/grade").
            r"\bP\d+\b|\b(?:SHERWIN|BENJAMIN MOORE)\b|\bPAINT\b(?!\s+(?:FINISH|FRAME|GRADE|TYPE|COAT|JOB))": "SF",
            # Generic flooring keywords last — RESILIENT/VINYL show up in
            # transition product names too, so we want the specific TR/TN
            # patterns above to fire first. \d* lets coded SKUs match
            # (LVT1, VCT2, FT3, CT4 ...).
            r"\b(LVT|VCT|FT|RF|SC|CT|SEALED CONCRETE|FLOOR TILE|RESILIENT|VINYL|CARPET)\d*\b": "SF",
        },
        "skip_predicates": ["empty_row"],
        "guidance": (
            "Finish schedule — emit ONE item per material code row "
            "(LVT1, P1, B2, TR3, WT4, etc.). Set is_quantifiable=true "
            "and use quantity='1' as a placeholder; the GC computes "
            "actual square/linear footage by measuring the floor plan. "
            "Pick the unit by material kind: SF for floor/wall tile, "
            "carpet, paint, gypsum, ceiling tile, acoustic panels; "
            "LF for transitions and wall base; EA for cabinets and "
            "fabricated pieces. Skip empty rows and pure header text."
        ),
    },
    # Plumbing/HVAC fixture schedule — one EA per tag (toilet, lav, urinal,
    # AHU, RTU, etc.); qty = explicit count from drawings or '1' as
    # placeholder
    "fixture": {
        "csi_division_hint": "22",
        "default_unit": "EA",
        "unit_overrides": {},
        "skip_predicates": ["empty_row"],
        "guidance": (
            "Plumbing/HVAC fixture schedule — every fixture tag is one bid "
            "item, unit=EA. Quantity per the row's count column or '1' as "
            "placeholder. Skip rows that are just header text."
        ),
    },
    # Equipment schedule — HVAC units, electrical equipment, kitchen, etc.
    "equipment": {
        "csi_division_hint": None,      # depends on equipment kind
        "default_unit": "EA",
        "unit_overrides": {
            # Lighting — emergency/exit signs etc. — count by EA
            r"\b(LED|LIGHTING|FIXTURE|EMERGENCY|EXIT SIGN)\b": "EA",
            # Bulk piping — refrigerant lines, ductwork mains, etc.
            r"\b(REFRIGERANT LINE|REFRIGERANT PIPING|DUCTWORK MAIN)\b": "LF",
        },
        # circuit_no_load filters SPARE/PROVISIONAL/RESERVED slots — relevant
        # because typed:equipment also covers electrical panels (PANEL: A1,
        # Panel Schedule, etc.) where those placeholder rows aren't bid items.
        "skip_predicates": ["empty_row", "circuit_no_load"],
        "guidance": (
            "Equipment schedule — every tagged unit is one bid item, "
            "unit=EA unless description names a piping system (LF). "
            "Skip rows with empty description fields, and skip rows whose "
            "description is a placeholder (SPARE, PROVISIONAL SPACE, "
            "RESERVED, FUTURE, NO LOAD) — those are not current bid items."
        ),
    },
    # Panel schedule — circuit-by-circuit table. Most rows are NOT bid items;
    # they're internal wiring assignments. ONLY emit items for rows that
    # describe a connected load worth bidding (e.g. specific equipment).
    "panel": {
        "csi_division_hint": "26",
        "default_unit": "EA",
        "unit_overrides": {},
        "skip_predicates": ["empty_row", "circuit_no_load"],
        "guidance": (
            "Panel schedule — IMPORTANT: most rows describe internal circuit "
            "assignments, NOT biddable scope items. A row like 'Circuit 8 = "
            "20A breaker, KITCHEN 116' is electrical wiring, not something a "
            "subcontractor bids separately. ONLY emit items when a row "
            "describes a SPECIFIC PIECE OF EQUIPMENT receiving power "
            "(e.g. 'Circuit 22 = RECIRC PUMP'). Mark is_quantifiable=false "
            "for circuit lookup tables that are pure wiring schematic."
        ),
    },
    # Plant/landscape schedule — count column + species + caliper
    "plant": {
        "csi_division_hint": "32",
        "default_unit": "EA",
        "unit_overrides": {},
        "skip_predicates": ["empty_row"],
        "guidance": (
            "Plant schedule — each row is one species at one size; quantity "
            "comes from the row's count/quantity column."
        ),
    },
    # Room schedule — references rooms by name; not a bid item itself
    "room": {
        "csi_division_hint": None,
        "default_unit": None,
        "unit_overrides": {},
        "skip_predicates": ["empty_row"],
        "guidance": (
            "Room schedule — rooms are reference data, NOT bid items. Set "
            "is_quantifiable=false."
        ),
    },
}


def _detect_schedule_type(schedule_name: str) -> str | None:
    """Pull the [typed:X] prefix out of the schedule name. None if untyped."""
    m = re.match(r"^\s*\[typed:([a-z]+)\]", schedule_name or "", re.IGNORECASE)
    return m.group(1).lower() if m else None


# Column keywords that identify a *catalog* schedule — defines material
# codes (B1, P1, LVT1) with manufacturer/spec details. Catalog rows ARE
# bid items.
_CATALOG_COL_KEYWORDS = {
    "code", "mark", "tag", "symbol", "designation",
    "item", "material", "product", "name",
    "manufacturer", "mfr", "brand", "supplier", "vendor",
    "description", "spec", "remarks", "size",
    "sku", "model", "pattern", "style", "collection",
}

# Column keywords that identify a *mapping* schedule — assigns codes to
# rooms (Room 101 → Floor LVT1, Base B1, Wall P1). Mapping rows are NOT
# bid items by themselves; the catalog schedule defines them and the
# floor-plan takeoff measures the quantity.
_MAPPING_COL_KEYWORDS = {
    "room", "room_number", "room_name", "room_no", "space",
    "floor", "flooring", "floor_finish",
    "base", "wall", "walls", "wall_finish", "wainscot",
    "ceiling", "ceilings", "ceiling_finish", "trim", "soffit",
}


def _classify_schedule_kind(columns: list[str] | None) -> str:
    """Inspect column names to decide what kind of schedule this is.

    Returns one of: 'catalog' (material-code rows), 'mapping' (room→code
    assignments), 'mixed' (both kinds of columns), 'unknown' (no clear
    signal — let the LLM decide row-by-row as before).
    """
    if not columns:
        return "unknown"
    cols = {(c or "").lower().replace("-", "_").strip() for c in columns}
    cols.discard("")
    catalog_hits = sum(
        1 for c in cols
        if c in _CATALOG_COL_KEYWORDS
        or any(c == kw or c.endswith("_" + kw) for kw in _CATALOG_COL_KEYWORDS)
    )
    mapping_hits = sum(
        1 for c in cols
        if c in _MAPPING_COL_KEYWORDS
        or any(c == kw or c.endswith("_" + kw) for kw in _MAPPING_COL_KEYWORDS)
    )
    if catalog_hits >= 2 and mapping_hits == 0:
        return "catalog"
    if mapping_hits >= 2 and catalog_hits == 0:
        return "mapping"
    if catalog_hits and mapping_hits:
        return "mixed"
    return "unknown"


def _row_is_empty(row: dict, columns: list[str]) -> bool:
    """A row is 'empty' when it has only a tag/index column populated."""
    populated = [c for c in columns if row.get(c) not in (None, "", "<UNKNOWN>")]
    if not populated:
        return True
    # Tag/identifier columns alone don't constitute a real row
    tag_cols = {"tag", "code", "row_index", "id", "ref"}
    non_tag = [c for c in populated if c.lower() not in tag_cols]
    return len(non_tag) == 0


def _row_is_color_only(row: dict, columns: list[str]) -> bool:
    """For finish schedules: a row is 'color only' if the ONLY non-trivial
    content is a manufacturer/SKU/color callout with no explicit quantity unit.

    The test is positive: row has spec/manufacturer keywords AND no
    measured-quantity pattern (digit immediately followed by SF/LF/CY/EA).
    SKU numbers like "SW 7005" don't count as quantity — only a digit
    paired with a unit token does.
    """
    text = " ".join(
        str(v) for v in (row.get(c, "") for c in columns) if v
    ).upper()
    if not text:
        return True
    # Explicit measured quantity present? Then it's quantifiable.
    if re.search(r"\b\d+(?:[\.,]\d+)?\s*(SF|LF|CY|EA|S\.F\.|L\.F\.|C\.Y\.)\b", text):
        return False
    # Otherwise, if any spec/manufacturer/SKU keyword is present, treat
    # the row as a material-callout (not a bid quantity).
    spec_keywords = (
        "COLOR", "SKU", "MANUFACTURER", "MFR", "PATTERN", "FINISH",
        "SHERWIN", "WILSON", "CROSSVILLE", "COBALT", "AKZO", "BENJAMIN",
    )
    return any(k in text for k in spec_keywords)


_CIRCUIT_NO_LOAD_TERMS = re.compile(
    r"\b(SPARE|PROVISIONAL\s+SPACE|PROVISIONAL|RESERVED|FUTURE|BLANK|"
    r"NO\s+LOAD|UNUSED|EMPTY)\b",
    re.IGNORECASE,
)


def _row_is_circuit_no_load(row: dict, columns: list[str]) -> bool:
    """Panel schedule row that's just a circuit assignment with no real load.

    Two cases skip:
      1. No description / load / remarks at all — pure wiring schematic.
      2. Description is a placeholder (SPARE / PROVISIONAL SPACE / RESERVED /
         FUTURE / NO LOAD) — circuit position reserved for later, not a
         current bid item.
    """
    desc_cols = [c for c in columns if c.lower() in ("description", "load", "remarks")]
    desc_text = " ".join(
        str(row.get(c) or "") for c in desc_cols
    ).strip()
    if not desc_text:
        return True
    return bool(_CIRCUIT_NO_LOAD_TERMS.search(desc_text))


_SKIP_PREDICATES = {
    "empty_row": _row_is_empty,
    "color_only_row": _row_is_color_only,
    "circuit_no_load": _row_is_circuit_no_load,
}


def _apply_unit_override(
    schedule_type: str | None,
    description: str,
    default: str,
) -> str:
    """If the description matches a known unit-override pattern, return
    the overridden unit. Else return default.

    Lookup rules:
      - typed schedule with non-empty unit_overrides → use those rules only.
        (Don't fall through to finish-rules; that bled into door/panel rows
        where "paint finish" / "resilient channel" would mis-fire.)
      - typed schedule with EMPTY unit_overrides ({}) → trust the schedule's
        default_unit (door=EA, fixture=EA, panel=EA, etc.).
      - untyped schedule (schedule_type is None) → use finish-rules as a
        content fallback. This is the case where transitions or base trim
        landed in an untyped schedule and would otherwise keep a wrong unit.
    """
    upper = description.upper()
    if schedule_type:
        rules = _TYPE_RULES.get(schedule_type, {}).get("unit_overrides") or {}
    else:
        rules = _TYPE_RULES.get("finish", {}).get("unit_overrides", {})

    for pattern, unit in rules.items():
        if re.search(pattern, upper, re.IGNORECASE):
            return unit
    return default


def _description_matches_row(description: str, row: dict, columns: list[str]) -> float:
    """Hallucination guard: how much of the description's word content
    actually appears in the source row?

    Returns a Jaccard-like overlap ratio in [0, 1]. We skip very common
    construction terms ("schedule", "see", "specification", etc.) so the
    check rewards specificity: a row about EH-1 in a heater schedule needs
    to share words like 'EH-1' or 'heater' with the description, not just
    'schedule'.
    """
    row_text = " ".join(
        str(v) for v in (row.get(c, "") for c in columns) if v not in (None, "")
    ).upper()
    if not row_text or not description:
        return 0.0

    common_words = {
        "THE", "AND", "FOR", "WITH", "OF", "TO", "PER", "AT", "IN", "ON", "BY",
        "FROM", "AS", "OR", "A", "AN", "IS", "BE", "SHALL", "PROVIDE", "INSTALL",
        "SCHEDULE", "SEE", "DETAIL", "SHEET", "SPEC", "SPECIFICATION",
    }
    # Include short codes like B1/P1/WT/TR — those carry more signal than
    # generic words. Min length of 2 with at least one alphanumeric.
    token_re = re.compile(r"\b[A-Z0-9][A-Z0-9-/.]*\b")
    desc_tokens = {
        t for t in token_re.findall(description.upper())
        if len(t) >= 2 and t not in common_words
    }
    row_tokens = {
        t for t in token_re.findall(row_text)
        if len(t) >= 2 and t not in common_words
    }
    if not desc_tokens:
        return 1.0  # No specific tokens to match — neutral
    overlap = desc_tokens & row_tokens
    return len(overlap) / len(desc_tokens)


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

    # L3 semantic split: when a finish schedule's columns describe a
    # room→code mapping (room/room_number/base/wall/ceiling), the rows
    # are NOT bid items — they're takeoff metadata that maps codes onto
    # locations. Emitting them as candidates produces phantom items
    # ("B2 flooring" from a row that just says room=B2). The catalog
    # version of the same schedule (CODE/ITEM/MANUFACTURER) survives
    # dedupe and emits the real bid items.
    schedule_kind = _classify_schedule_kind(s.columns)
    schedule_type_pre = _detect_schedule_type(s.name)
    if schedule_type_pre == "finish" and schedule_kind == "mapping":
        log.info(
            "schedule_miner: skipping mapping-shape finish schedule %r "
            "(columns=%s) — room→code data, not bid items",
            s.name, s.columns,
        )
        return [], Usage(), 0

    prompt_body = _format_schedule_for_prompt(s, ctx.sheet_number)

    # Layer 1: type-aware prompt augmentation
    schedule_type = _detect_schedule_type(s.name)
    type_rule = _TYPE_RULES.get(schedule_type or "")
    type_guidance = ""
    if type_rule and type_rule.get("guidance"):
        type_guidance = (
            f"\n\nSchedule type: {schedule_type}\n"
            f"Type-specific guidance: {type_rule['guidance']}"
        )
        if type_rule.get("csi_division_hint"):
            type_guidance += (
                f"\nLikely CSI division: {type_rule['csi_division_hint']}"
            )
        if type_rule.get("default_unit"):
            type_guidance += (
                f"\nDefault unit for this schedule type: "
                f"{type_rule['default_unit']}"
            )

    prompt = (
        f"{prompt_body}{type_guidance}\n\n"
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
    if type_rule and type_rule.get("default_unit"):
        # Trust the type rule's unit if Haiku didn't pick something specific
        default_unit = (
            (it_unit := payload.get("default_unit")) and it_unit.strip()
        ) or type_rule["default_unit"]

    # Layer 1 row filtering: which skip-predicates apply for this type
    skip_predicate_names = (
        type_rule.get("skip_predicates", []) if type_rule else []
    )
    active_predicates = [
        _SKIP_PREDICATES[name]
        for name in skip_predicate_names
        if name in _SKIP_PREDICATES
    ]

    candidates: list[CandidateItem] = []
    skipped_empty = 0
    skipped_hallucinated = 0
    overridden_units = 0

    for it in payload.get("items", []):
        row_index = int(it.get("row_index", -1))
        if row_index < 0 or row_index >= len(s.rows):
            continue
        description = (it.get("description") or "").strip()
        if not description:
            continue

        row = s.rows[row_index]

        # Filter 1 — type-aware row skip predicates
        if any(pred(row, s.columns) for pred in active_predicates):
            skipped_empty += 1
            continue

        # Filter 2 — hallucination guard. The description must overlap with
        # the source row's content above a low threshold. Without this guard
        # we saw cases where the AI invented a "T-bar LED ceiling grid"
        # description for a row that was actually about emergency exit signs.
        # 0.20 is intentionally lenient — only catches gross mismatches.
        overlap = _description_matches_row(description, row, s.columns)
        if overlap < 0.20:
            skipped_hallucinated += 1
            log.info(
                "schedule_miner: skipping hallucinated row %d (overlap=%.2f) "
                "in %s — desc='%s'",
                row_index, overlap, s.name, description[:60],
            )
            continue

        quantity = it.get("quantity") or "1"
        haiku_unit = (it.get("unit") or default_unit).strip() or default_unit

        # Filter 3 — type-aware unit override. If the row description
        # matches a known pattern for this schedule type, force the
        # right unit (FT1 floor tile → SF, B2 base → LF, etc.).
        unit = _apply_unit_override(schedule_type, description, haiku_unit)
        if unit != haiku_unit:
            overridden_units += 1
            log.info(
                "schedule_miner: unit override on row %d: %s → %s (type=%s, desc='%s')",
                row_index, haiku_unit, unit, schedule_type, description[:50],
            )

        specification = it.get("specification")

        synthetic_chunk = _build_synthetic_row_chunk(
            ctx.chunk,
            schedule_name=s.name,
            row_index=row_index,
            row=row,
            columns=s.columns,
        )
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

    if skipped_empty or skipped_hallucinated or overridden_units:
        log.info(
            "schedule_miner: %s (%s) — %d items, %d skipped (empty), "
            "%d skipped (hallucinated), %d unit overrides",
            s.name, schedule_type or "untyped", len(candidates),
            skipped_empty, skipped_hallucinated, overridden_units,
        )

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

        # Dedupe — the schedule extractor sometimes runs both an untyped pass
        # and a typed (`[typed:X]`) pass over the same source page, so the
        # same logical schedule appears twice with different IDs.
        #
        # Selection priority for the chosen pass:
        #   1. catalog-shaped columns (CODE/ITEM/MANUFACTURER/...) — emits
        #      one bid item per material code.
        #   2. typed pass — better type-aware prompting.
        #   3. anything else.
        # Mapping-shaped passes are deprioritized because the typed extractor
        # sometimes mis-routes catalog data into a room/floor/base/wall/
        # ceiling schema, garbling the column→content alignment.
        def _strip_type_prefix(name: str) -> str:
            return re.sub(r"^\s*\[typed:[^\]]+\]\s*", "", name or "", flags=re.IGNORECASE).strip().lower()

        def _selection_score(s: ExtractedSchedule) -> tuple[int, int]:
            kind = _classify_schedule_kind(s.columns)
            kind_rank = {"catalog": 3, "unknown": 2, "mixed": 1, "mapping": 0}[kind]
            typed_rank = 1 if (s.name or "").lstrip().lower().startswith("[typed:") else 0
            return (kind_rank, typed_rank)

        groups: dict[tuple, list] = defaultdict(list)
        for row in schedules:
            s = row[0]
            key = (s.page_extraction_id, _strip_type_prefix(s.name))
            groups[key].append(row)

        deduped: list = []
        dropped = 0
        for key, group in groups.items():
            if len(group) == 1:
                deduped.append(group[0])
                continue
            chosen = max(group, key=lambda r: _selection_score(r[0]))
            deduped.append(chosen)
            dropped += len(group) - 1
            log.info(
                "schedule_miner: dedupe pick for page %s '%s' — kept %s (%s) "
                "out of %d candidates",
                chosen[0].page_extraction_id,
                key[1],
                chosen[0].name,
                _classify_schedule_kind(chosen[0].columns),
                len(group),
            )
        if dropped:
            log.info(
                "schedule_miner: deduped %d redundant schedule pass(es) "
                "(%d → %d schedules)",
                dropped, len(schedules), len(deduped),
            )
        schedules = deduped

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
