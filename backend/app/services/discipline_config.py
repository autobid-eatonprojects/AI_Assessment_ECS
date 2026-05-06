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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

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
