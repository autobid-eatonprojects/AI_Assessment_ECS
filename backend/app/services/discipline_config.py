"""P3 — discipline mapping resolver.

Loads `app/data/discipline_mapping.yaml` and exposes:
  - Which discipline a CSI division belongs to
  - The full set of disciplines + their owned divisions
  - Sheet-prefix → discipline rules (A* → architectural, S* → structural,
    etc.) for the Sheet Index extractor's discipline tag, since some
    cover sheets don't list disciplines explicitly

Per design doc: 9 discipline agents (civil, site, architectural,
interior, structural, fire-protection, plumbing, mechanical, electrical)
plus a 'general' bucket for Div 00/01 administrative items and an
'equipment-special' bucket for Div 11/13/14.

Single source of truth — every consumer that needs to know "which
discipline owns CSI Div 03" reads from here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


# Discipline names AND drawing-jargon abbreviations that the vision extractor
# sometimes captures verbatim as `target_sheet` when a drawing note reads
# "see Architectural drawings", "REFER TO SITE", or "SEE ARCHITECT'S RCP".
# They look like sheet IDs (uppercase, no spaces after stripping suffixes)
# but they reference a discipline, drawing TYPE, or piece of equipment —
# not an actual sheet number. Treated as not-a-sheet so they don't generate
# unresolved_cross_reference gaps.
_DISCIPLINE_KEYWORDS: frozenset[str] = frozenset({
    # Discipline names
    "STRUCTURAL", "STRUCT", "ARCHITECTURAL", "ARCH", "CIVIL",
    "MECHANICAL", "ELECTRICAL", "ELEC", "PLUMBING", "PLUMB",
    "INTERIOR", "LANDSCAPE", "FIRE PROTECTION", "FIRE", "MEP",
    "GENERAL", "HVAC", "SITE",
    # Drawing-type abbreviations (refer to a kind of drawing, not a sheet)
    "RCP", "FFP", "FP", "ELEV", "ELEVATIONS", "PLAN", "PLANS",
    "DETAIL", "DETAILS", "SECTION", "SECTIONS", "SCHEDULE", "SCHEDULES",
    # Equipment/abbreviation false positives ("see FACP" = the device,
    # not a sheet labeled FACP)
    "FACP", "FAAP", "AHU", "VAV", "RTU",
    # Generic markers
    "TYP", "TYPICAL", "SIM", "SIMILAR", "NIC", "NTS", "OPP", "OPPOSITE",
    "ABOVE", "BELOW", "THIS", "OTHER", "OTHERS",
})

# Real AEC sheet IDs: 1-3 alpha discipline prefix, optional dash/space, then
# digit-based identifier with optional dotted/dashed sub-sheet. Examples that
# match: A1.1, A0.0, AS1.1, S0.1, FP0.1, C001, C100, M0.1, ES0.1, T-101.
# Examples that don't: STRUCTURAL, ARCHITECTURAL, MECHANICAL DRAWINGS.
_SHEET_ID_RE = re.compile(r"^[A-Z]{1,3}\-?\d{1,4}([.\-]\d{1,3})?[A-Z]?$")

# Short letters-only sheets — covers cover sheets (CVR), title (TS, T1 if
# the digit form fails), index (INX). Capped at 4 chars so it doesn't admit
# discipline-name false positives like ARCH, ELEC, MEP.
_SHORT_ALPHA_RE = re.compile(r"^[A-Z]{1,4}$")

# Narrative phrase suffixes that show up after discipline names in drawing
# notes — strip before classifying so "STRUCTURAL DRAWINGS" → "STRUCTURAL".
_PHRASE_SUFFIXES: tuple[str, ...] = (
    " DRAWINGS", " DRAWING", " DWG", " DWGS", " PLAN", " PLANS",
    " SHEET", " SHEETS", " SET",
)


def is_valid_sheet_id(value: str | None) -> bool:
    """Return True iff `value` looks like a real AEC sheet identifier.

    Filters discipline-name false positives (STRUCTURAL, ARCHITECTURAL, etc.)
    that the vision extractor captures when a drawing note reads "see
    Structural drawings" instead of "see S2.1". These were inflating the
    unresolved_cross_reference gap count without representing real broken
    links.
    """
    if not value:
        return False
    s = re.sub(r"\s+", " ", value).strip().upper()
    for suf in _PHRASE_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)].rstrip()
    if not s or s in _DISCIPLINE_KEYWORDS:
        return False
    if _SHEET_ID_RE.match(s):
        return True
    if _SHORT_ALPHA_RE.match(s):
        # Short letters-only string that survived the discipline-keyword
        # filter — likely CVR / TS / similar cover-style sheet code.
        return True
    return False

_MAPPING_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "discipline_mapping.yaml"
)


# Sheet-prefix → discipline. AEC convention; cover sheets that don't
# spell out the discipline can be inferred from these prefixes.
SHEET_PREFIX_DISCIPLINE: dict[str, str] = {
    "A": "architectural",
    "S": "structural",
    "M": "mechanical",
    "E": "electrical",
    "P": "plumbing",
    "FP": "fire-protection",
    "C": "civil",
    "L": "landscape",   # falls back to 'site' downstream
    "I": "interior",
    "G": "general",
    "T": "general",     # title sheets are general
}


@dataclass(frozen=True)
class Discipline:
    key: str
    label: str
    csi_divisions: tuple[str, ...]


@lru_cache(maxsize=1)
def load_disciplines() -> tuple[Discipline, ...]:
    if not _MAPPING_PATH.exists():
        log.warning(
            "discipline_config: %s not found; returning empty mapping",
            _MAPPING_PATH,
        )
        return ()
    with _MAPPING_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out: list[Discipline] = []
    for entry in data.get("disciplines") or []:
        key = (entry.get("key") or "").strip()
        label = (entry.get("label") or "").strip()
        divs = tuple(str(d).strip().zfill(2) for d in (entry.get("csi_divisions") or []))
        if key and divs:
            out.append(Discipline(key=key, label=label, csi_divisions=divs))
    return tuple(out)


@lru_cache(maxsize=1)
def division_to_discipline_map() -> dict[str, Discipline]:
    """Flat map: csi_division → owning Discipline. Errors fast on duplicates."""
    out: dict[str, Discipline] = {}
    for d in load_disciplines():
        for div in d.csi_divisions:
            if div in out and out[div].key != d.key:
                raise ValueError(
                    f"discipline_config: division {div} mapped to both "
                    f"{out[div].key!r} and {d.key!r}"
                )
            out[div] = d
    return out


def discipline_for_division(csi_division: str) -> Discipline | None:
    """Return the Discipline that owns this CSI division, or None."""
    return division_to_discipline_map().get(csi_division.zfill(2))


def discipline_for_sheet_prefix(sheet_id: str) -> str | None:
    """AEC-convention discipline tag from a sheet ID's prefix.

    'A1.1' → 'architectural', 'S2.3' → 'structural', 'FP-101' → 'fire-protection'.
    Used by sheet_index_extractor when the cover sheet didn't tag a row.
    """
    if not sheet_id:
        return None
    s = sheet_id.upper().lstrip()
    # Two-char prefixes first (FP)
    if s.startswith("FP"):
        return SHEET_PREFIX_DISCIPLINE.get("FP")
    return SHEET_PREFIX_DISCIPLINE.get(s[:1])


def disciplines_for_project(active_divisions: set[str]) -> list[Discipline]:
    """Pick the disciplines whose divisions intersect this project's
    relevant CSI divisions.

    Used by the P3 orchestrator to know which discipline agents to spawn
    for a given project — no point spawning a Mechanical agent if Div 23
    isn't relevant.
    """
    seen: set[str] = set()
    result: list[Discipline] = []
    for div in active_divisions:
        d = discipline_for_division(div)
        if d is None or d.key in seen:
            continue
        seen.add(d.key)
        result.append(d)
    return result
