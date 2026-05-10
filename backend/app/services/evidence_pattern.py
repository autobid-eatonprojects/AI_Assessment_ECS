"""Per-item *expected* evidence pattern.

Not every scope item should be bilateral (have BOTH a spec citation and a
drawing citation). Construction reality:

  - Division 1 General Requirements (submittals, mockups, QC, closeout)
    are NEVER drawn — they're 100% spec content. Demanding a drawing
    citation for them creates false-alarm gaps.
  - Demolition extent and existing-condition tie-ins are typically
    drawing-only — the spec rarely repeats what's shown on demo plans.
  - Material/equipment items (concrete, doors, HVAC, etc.) genuinely
    need both — the spec defines the *what*, the drawing defines the
    *where/how-much*.

This module is a pure deterministic classifier. Inputs are an item's
CSI code and description; output is the expected pattern. The coverage
rollup uses this to compute "did the item meet its expected pattern?"
instead of a blanket "does it have both sides?".

No persistence — this is recomputed on the fly each time we roll up
coverage. The classification depends only on csi_code + description,
both already on every ScopeItem.
"""

from __future__ import annotations

import re
from typing import Literal

EvidencePattern = Literal["bilateral", "spec_only", "drawing_only"]

DEFAULT_PATTERN: EvidencePattern = "bilateral"


# Keywords in the item description that force spec_only regardless of
# CSI division. Catches admin/QC items the LLM placed under a material
# division by mistake. Match is whole-word, case-insensitive.
_ADMIN_KEYWORDS = (
    "submittal", "submittals", "mockup", "mockups", "warranty",
    "warranties", "closeout", "punch list",
    "shop drawing", "shop drawings",
    "quality control", "quality assurance",
    "testing requirement", "testing requirements", "test report",
    "permit", "permits", "bond", "bonds",
    "mobilization", "demobilization",
    "cleanup", "clean-up", "general cleanup", "final cleaning",
    "dumpster", "dumpsters", "site clean",
    "supervision", "general conditions", "overhead",
    "as-built", "as-builts", "operation and maintenance manual",
    "o&m manual", "o&m manuals",
    "schedule of values", "pay application",
    "insurance", "indemnification",
)

_ADMIN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _ADMIN_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def is_admin_description(description: str | None) -> bool:
    """True if the item description contains admin/QC/closeout boilerplate.

    Public helper — used by `expected_pattern` (force spec_only) AND by
    conflict_resolution's cross-division clustering (admin scope is
    inherently per-division, so admin items should not cluster across
    divisions even when their boilerplate phrasing is similar — e.g. "Product
    data submittal for X" appears once in every division and clustering them
    as cross-division overlap is pure noise).
    """
    if not description:
        return False
    return bool(_ADMIN_RE.search(description))


# CSI section / division → expected pattern.
#
# Lookup priority (caller logic):
#   1. exact section match (NN NN NN)
#   2. NN NN 00 family match
#   3. division (NN) match
#   4. default = bilateral
#
# Conventions used in this table — based on standard MasterFormat usage,
# not project-specific. If you adjust, update unit tests.
_SECTION_OVERRIDES: dict[str, EvidencePattern] = {
    # ---- Division 02 — Existing Conditions ----
    # Demolition extent is shown on demo plans; spec rarely repeats it.
    "02 41 00": "drawing_only",   # Demolition (general)
    "02 41 13": "drawing_only",   # Selective Site Demolition
    "02 41 16": "drawing_only",   # Structure Demolition
    "02 41 19": "drawing_only",   # Selective Demolition (interior)
    "02 42 00": "drawing_only",   # Removal and Salvage of Construction Materials

    # ---- Division 31 — Earthwork ----
    "31 10 00": "drawing_only",   # Site Clearing
    "31 11 00": "drawing_only",   # Clearing and Grubbing
    # 31 23 00 Excavation/Fill stays bilateral (spec mix + drawing extent)

    # ---- Division 33 — Utilities ----
    # Existing utility tie-ins are drawing call-outs. New utility runs are
    # bilateral (spec defines pipe class, drawing locates the run). The
    # overrides here cover only existing/tie-in connection sections.
    "33 05 33.13": "drawing_only",  # Existing Utility Identification
}


# Division → expected pattern for items that don't match a section override.
_DIVISION_DEFAULT: dict[str, EvidencePattern] = {
    "01": "spec_only",   # General Requirements — admin, QC, submittals, closeout
    # All other divisions default to "bilateral" via DEFAULT_PATTERN.
}


def _normalize_csi(csi: str) -> str:
    """Strip whitespace and uppercase. Returns '' for None/empty."""
    return (csi or "").strip().upper()


def _csi_division(csi: str) -> str:
    """Return the leading 2-digit division ('01', '03', '26'), or ''."""
    s = _normalize_csi(csi)
    m = re.match(r"^(\d{2})", s)
    return m.group(1) if m else ""


def _csi_family(csi: str) -> str:
    """Return 'NN NN 00' family code or '' if csi is malformed."""
    s = _normalize_csi(csi)
    m = re.match(r"^(\d{2})\s*(\d{2})", s)
    if not m:
        return ""
    return f"{m.group(1)} {m.group(2)} 00"


def expected_pattern(csi_code: str, description: str | None = None) -> EvidencePattern:
    """Return the evidence pattern this item should be measured against.

    Lookup order:
      1. admin keywords in description → spec_only (overrides everything)
      2. exact CSI section match in _SECTION_OVERRIDES
      3. NN NN 00 family match
      4. division NN match in _DIVISION_DEFAULT
      5. DEFAULT_PATTERN (bilateral)
    """
    # 1. Admin override
    if description and _ADMIN_RE.search(description):
        return "spec_only"

    csi = _normalize_csi(csi_code)
    if not csi:
        return DEFAULT_PATTERN

    # 2. Exact section
    if csi in _SECTION_OVERRIDES:
        return _SECTION_OVERRIDES[csi]

    # 3. Family
    fam = _csi_family(csi)
    if fam and fam != csi and fam in _SECTION_OVERRIDES:
        return _SECTION_OVERRIDES[fam]

    # 4. Division
    div = _csi_division(csi)
    if div in _DIVISION_DEFAULT:
        return _DIVISION_DEFAULT[div]

    return DEFAULT_PATTERN


def matches_expected(
    pattern: EvidencePattern, has_spec: bool, has_drawing: bool
) -> bool:
    """Did the item's actual evidence meet its expected pattern?"""
    if pattern == "spec_only":
        return has_spec
    if pattern == "drawing_only":
        return has_drawing
    return has_spec and has_drawing  # bilateral
