"""P4 — industry-canonical symbol → CSI lookup (W3 mitigation continued).

Bootstraps the project-specific SymbolLegend with a baseline mapping
of common AEC drawing symbols (WC, AHU, FACP, LP, etc.) to CSI 6-digit
sections. Used when:

  - SymbolLegend.csi_section_hint is empty (vision pass didn't tag one)
    → backfilled from this dictionary if the glyph matches a canonical
  - The discipline_agent needs a baseline understanding of project
    symbols even before any legend has been extracted
  - The link_judge wants a tier-3 (WEAK) signal that "the chunk
    references a symbol whose canonical CSI matches the item's CSI"

Project-specific SymbolLegend rows always win — this is the FALLBACK,
not the override. Many symbols are context-dependent: 'DP' is a
distribution panel in Electrical and a dry pipe valve in Fire
Protection; lookup is keyed by (glyph, discipline) when discipline
is known.

Source: app/data/symbol_to_csi.yaml.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

log = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).parent.parent / "data" / "symbol_to_csi.yaml"


@dataclass(frozen=True)
class SymbolExpansion:
    glyph: str
    csi_section: str
    meaning: str
    discipline: str | None


def _normalize_glyph(g: str | None) -> str:
    """Canonicalize a glyph for lookup. Upper-case + strip non-alnum
    except for & / - / . — those are meaningful in symbols like 'OS&Y'."""
    if not g:
        return ""
    s = g.upper().strip()
    # Collapse internal whitespace
    s = re.sub(r"\s+", " ", s)
    return s


@lru_cache(maxsize=1)
def _load_table() -> dict[str, list[SymbolExpansion]]:
    """Read the YAML once per process and index by normalized glyph."""
    if not _DATA_FILE.exists():
        log.warning("symbol_to_csi: %s not found; lookup disabled", _DATA_FILE)
        return {}
    with _DATA_FILE.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: dict[str, list[SymbolExpansion]] = {}
    for entry in raw.get("symbols") or []:
        glyph = _normalize_glyph(entry.get("glyph"))
        if not glyph:
            continue
        for exp in entry.get("expansions") or []:
            csi = (exp.get("csi_section") or "").strip()
            meaning = (exp.get("meaning") or "").strip()
            disc = (exp.get("discipline") or "").strip().lower() or None
            if not csi or not meaning:
                continue
            out.setdefault(glyph, []).append(
                SymbolExpansion(
                    glyph=glyph,
                    csi_section=csi,
                    meaning=meaning,
                    discipline=disc,
                )
            )
    log.info(
        "symbol_to_csi: loaded %d glyphs (%d total expansions) from %s",
        len(out), sum(len(v) for v in out.values()), _DATA_FILE,
    )
    return out


def lookup(glyph: str | None, *, discipline: str | None = None) -> list[SymbolExpansion]:
    """Return every canonical expansion for this glyph.

    If `discipline` is given, prefer (then filter to) expansions tagged
    for that discipline. Returns an empty list if the glyph isn't
    canonicalised in the seed table — callers should fall back to the
    project's SymbolLegend.
    """
    table = _load_table()
    candidates = table.get(_normalize_glyph(glyph)) or []
    if not candidates:
        return []
    if discipline:
        d = discipline.strip().lower()
        same_disc = [c for c in candidates if c.discipline == d]
        if same_disc:
            return same_disc
    return list(candidates)


def best_csi(glyph: str | None, *, discipline: str | None = None) -> str | None:
    """Single-best CSI section guess for a glyph.

    When multiple expansions tie (no discipline provided + glyph is
    multi-context), return None so the caller doesn't pin to a
    misleading code. Use lookup() for the full set.
    """
    candidates = lookup(glyph, discipline=discipline)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].csi_section
    # Multiple candidates and no discipline disambiguation — refuse
    # to guess; let the caller decide.
    return None


def backfill_legend_hints(rows: Iterable) -> int:
    """Backfill SymbolLegend.csi_section_hint for rows missing one.

    Mutates the SQLAlchemy SymbolLegend objects in-place; caller
    commits. Returns the number of rows updated.
    """
    n = 0
    for r in rows:
        if r.csi_section_hint:
            continue
        guess = best_csi(r.symbol_glyph, discipline=r.discipline)
        if guess:
            r.csi_section_hint = guess
            n += 1
    return n


def render_canonical_block_for_discipline(discipline: str) -> str:
    """Render the canonical symbol table as a prompt block.

    Used by the discipline_agent to seed its context with a baseline
    symbol→CSI mapping when the project's SymbolLegend is empty.
    """
    table = _load_table()
    rows: list[str] = []
    d = discipline.strip().lower()
    for glyph, expansions in sorted(table.items()):
        for exp in expansions:
            if exp.discipline == d:
                rows.append(
                    f"  {glyph:<10} = {exp.meaning} [csi: {exp.csi_section}]"
                )
                break
    return "\n".join(rows)
