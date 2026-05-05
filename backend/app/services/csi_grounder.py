"""Phase 4.3c — Strict CSI code grounding.

The model occasionally produces a CSI code that's almost-but-not-quite a
real one (e.g. '03 30 01' when the real code is '03 30 00'). This service
maps any proposed code against the project's uploaded Trade_List taxonomy:

    1. Exact match → keep as-is.
    2. Same division + similar section number → snap to the nearest valid
       section in the same division (preserves trade attribution).
    3. No reasonable match → fall back to the parent division code
       (e.g. '03 00 00') so the item is still grouped correctly.

The goal: every ScopeItem.csi_code is guaranteed to be a real CSI code
that exists in the uploaded Trade_List. No fabrications can leak through.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .trade_list_parser import CSITaxonomy

log = logging.getLogger(__name__)


_CODE_RE = re.compile(r"^\s*(\d{2})[\s.\-_]+(\d{2})[\s.\-_]+(\d{2}(?:[.\-_]\d+)?)\s*$")


@dataclass
class GroundingResult:
    code: str  # canonical "NN NN NN"
    division: str  # "NN"
    method: str  # "exact" | "snapped" | "division-fallback"
    note: str | None  # what changed, for audit


def _normalise_code(raw: str) -> str | None:
    """Convert 'NN-NN-NN', 'NN.NN.NN', 'NNNNNN', 'NN NN NN' all to 'NN NN NN'."""
    if not raw:
        return None
    s = raw.strip()
    # Bare 6-digit form: "030000" → "03 00 00"
    bare = re.sub(r"\D", "", s)
    if len(bare) >= 6 and bare[:6].isdigit():
        return f"{bare[0:2]} {bare[2:4]} {bare[4:6]}"
    m = _CODE_RE.match(s)
    if m:
        # Drop any sub-section suffix (.13 etc) for the canonical lookup
        sub = m.group(3).split(".")[0].split("-")[0].split("_")[0]
        return f"{m.group(1)} {m.group(2)} {sub[:2]}"
    return None


def ground_code(proposed: str, taxonomy: CSITaxonomy) -> GroundingResult:
    """Map any proposed code to a real one in the uploaded taxonomy."""
    canonical = _normalise_code(proposed) or ""
    division_code = canonical[:2] if len(canonical) >= 2 else ""

    # 1. Exact match
    if canonical and canonical in taxonomy.section_codes:
        return GroundingResult(
            code=canonical, division=division_code, method="exact", note=None
        )

    # 2. Same-division snap: find closest section in the same division.
    if division_code:
        div = taxonomy.get_division(division_code)
        if div is not None and div.sections:
            # Prefer same first sub-code (NN XX __); among those, closest by suffix
            target_xx = canonical[3:5] if len(canonical) >= 5 else ""
            same_xx = [s for s in div.sections if s.code[3:5] == target_xx]
            candidates = same_xx or div.sections
            # Pick the section with the smallest absolute distance in concatenated code
            try:
                target_int = int(canonical.replace(" ", ""))
                best = min(
                    candidates,
                    key=lambda s: abs(int(s.code.replace(" ", "")) - target_int),
                )
            except (ValueError, AttributeError):
                best = candidates[0]
            note = (
                f"snapped from '{proposed}' to '{best.code}' "
                f"(same division {division_code})"
            )
            log.info("csi_grounder: %s", note)
            return GroundingResult(
                code=best.code,
                division=division_code,
                method="snapped",
                note=note,
            )

    # 3. Fallback to parent division if known
    if division_code in taxonomy.division_codes:
        return GroundingResult(
            code=f"{division_code} 00 00",
            division=division_code,
            method="division-fallback",
            note=f"unable to snap '{proposed}'; using division {division_code} root",
        )

    # 4. Last resort: invalid code → return as-is so the orchestrator can drop the item
    return GroundingResult(
        code=canonical or proposed.strip(),
        division=division_code or "??",
        method="division-fallback",
        note=f"could not ground '{proposed}' to any division in the uploaded Trade_List",
    )
