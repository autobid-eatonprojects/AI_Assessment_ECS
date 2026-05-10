"""Deterministic citation validator — runs before persistence.

Filters out citations whose source chunk is structurally insufficient to
count as evidence. Three rejection rules:

  EMPTY        — chunk has no extractable text
  HEADER_ONLY  — chunk text is just a sheet header / title block
                 (e.g. "S1.1 - FOUNDATION PLAN") with no body content
  LOW_OVERLAP  — chunk text shares almost no specific tokens with the
                 item description (e.g. a cite to "GENERAL NOTES" page
                 for a 4" concrete slab item)

This is the cheap deterministic gate. It runs *before* Haiku link_judge
so we don't burn tokens checking obvious garbage. Items that lose all
citations on one side keep what they have left; bilateral_evidence
recomputes from the surviving citations.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Sheet-id patterns (e.g. "S1.1", "A2.1", "E0.3", "C100"). Allow either a
# `-`/`—`/`:` separator or just whitespace before a short trailing title.
_SHEET_ID_BARE = re.compile(r"^[A-Z]{1,4}\d+(?:\.\d+)?$", re.IGNORECASE)
_SHEET_LINE = re.compile(
    r"^[A-Z]{1,4}\d+(?:\.\d+)?(?:\s*[-—:]?\s+[A-Z0-9 ,&/]+)?$",
    re.IGNORECASE,
)

# Words that appear in sheet headers / title blocks. A chunk made entirely
# of these (plus optional sheet ids) is almost certainly a header.
_HEADER_KEYWORDS = frozenset({
    # Plan types
    "PLAN", "PLANS", "SCHEDULE", "SCHEDULES", "DETAIL", "DETAILS",
    "ELEVATION", "ELEVATIONS", "SECTION", "SECTIONS", "LEGEND",
    "INDEX", "COVER", "GENERAL", "NOTES", "SHEET", "DRAWING",
    "DRAWINGS", "TITLE", "BLOCK", "PARTIAL",
    # Disciplines / building parts that appear in titles
    "FOUNDATION", "FOUNDATIONS", "FRAMING", "ROOF", "FLOOR",
    "FLOORS", "CEILING", "CEILINGS", "REFLECTED", "EXTERIOR",
    "INTERIOR", "STRUCTURAL", "ARCHITECTURAL", "ELECTRICAL",
    "PLUMBING", "MECHANICAL", "CIVIL", "FIRE", "PROTECTION",
    "LANDSCAPE", "SITE", "LIFE", "SAFETY", "EGRESS", "ENERGY",
    "DEMOLITION", "ENLARGED", "OVERALL", "KEY", "WALL", "WALLS",
    "BUILDING", "DOOR", "DOORS", "WINDOW", "WINDOWS", "FINISH",
    "FINISHES", "CABINET", "CABINETS", "MILLWORK", "STAIR",
    "STAIRS", "RESTROOM", "RESTROOMS", "EQUIPMENT", "FIXTURE",
    "FIXTURES", "POWER", "LIGHTING", "PANEL", "PANELS",
    "DIAGRAM", "DIAGRAMS", "RISER", "ISOMETRIC",
    # Order words in floor names
    "FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "GROUND",
    "BASEMENT", "ATTIC", "MEZZANINE",
    # Common cover-page words
    "PROJECT", "MANUAL", "VOLUME", "SET", "REVISIONS", "REVISION",
    "ISSUED", "ISSUE", "DATE", "BID", "PERMIT", "CONSTRUCTION",
    "FOR",
})

# Tokens we ignore when computing description↔chunk overlap. Same idea
# as the schedule miner's hallucination guard.
_COMMON_TOKENS = frozenset({
    "THE", "AND", "FOR", "WITH", "OF", "TO", "PER", "AT", "IN", "ON",
    "BY", "FROM", "AS", "OR", "A", "AN", "IS", "BE", "SHALL", "PROVIDE",
    "INSTALL", "SCHEDULE", "SEE", "DETAIL", "SHEET", "SPEC",
    "SPECIFICATION", "DRAWINGS", "GENERAL", "NOTES", "PROJECT", "WORK",
})

_TOKEN_RE = re.compile(r"\b[A-Z0-9][A-Z0-9\-/.]*\b")

# Default tunables — generous on purpose. The validator catches obvious
# garbage; link_judge handles semantic adjudication.
DEFAULT_MIN_OVERLAP = 0.10
DEFAULT_HEADER_MAX_LEN = 120


@dataclass
class CitationVerdict:
    valid: bool
    reason: str  # "" if valid, else "EMPTY" | "HEADER_ONLY" | "LOW_OVERLAP"
    overlap: float  # 0.0 .. 1.0


def _tokenize(text: str) -> set[str]:
    return {
        t for t in _TOKEN_RE.findall((text or "").upper())
        if len(t) >= 2 and t not in _COMMON_TOKENS
    }


def _looks_like_sheet_header(text: str, *, max_len: int = DEFAULT_HEADER_MAX_LEN) -> bool:
    """True iff `text` is a sheet/title-block header with no body content.

    Strategy: short chunk where every line is either a sheet-id pattern
    or contains only header keywords (PLAN/SCHEDULE/DETAIL/etc.).
    """
    s = (text or "").strip()
    if not s:
        return True
    if len(s) > max_len:
        return False
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if not lines:
        return True
    # Bare sheet id alone
    if len(lines) == 1 and _SHEET_LINE.match(lines[0]):
        return True
    if len(lines) > 4:
        return False
    # All lines are header-like
    for ln in lines:
        if _SHEET_LINE.match(ln) or _SHEET_ID_BARE.match(ln):
            continue
        # Strip punctuation, see if remaining tokens are all header keywords
        toks = _TOKEN_RE.findall(ln.upper())
        if not toks:
            return False
        if not all(t in _HEADER_KEYWORDS for t in toks):
            return False
    return True


def validate_citation(
    item_description: str,
    chunk_text: str,
    *,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    header_max_len: int = DEFAULT_HEADER_MAX_LEN,
) -> CitationVerdict:
    """Decide if `chunk_text` is structurally valid evidence for the item."""
    if not chunk_text or not chunk_text.strip():
        return CitationVerdict(valid=False, reason="EMPTY", overlap=0.0)
    if _looks_like_sheet_header(chunk_text, max_len=header_max_len):
        return CitationVerdict(valid=False, reason="HEADER_ONLY", overlap=0.0)
    desc_tokens = _tokenize(item_description)
    chunk_tokens = _tokenize(chunk_text)
    if not desc_tokens:
        # Item description has no specific tokens — be lenient
        return CitationVerdict(valid=True, reason="", overlap=1.0)
    overlap_n = len(desc_tokens & chunk_tokens)
    overlap = overlap_n / len(desc_tokens)
    if overlap < min_overlap:
        return CitationVerdict(valid=False, reason="LOW_OVERLAP", overlap=overlap)
    return CitationVerdict(valid=True, reason="", overlap=overlap)


def filter_valid_citations(
    item_description: str,
    retrieved_chunks: list,
    *,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    header_max_len: int = DEFAULT_HEADER_MAX_LEN,
) -> tuple[list, list[tuple]]:
    """Split a list of citation-bearing objects into (kept, dropped).

    Each input object is expected to have either `.chunk.text` (RetrievedChunk
    shape) or `.text` directly. Returns:
        kept    — same shape as input
        dropped — list of (input, CitationVerdict) for log/audit
    """
    kept: list = []
    dropped: list[tuple] = []
    for r in retrieved_chunks:
        chunk = getattr(r, "chunk", r)
        text = getattr(chunk, "text", "") or ""
        verdict = validate_citation(
            item_description, text,
            min_overlap=min_overlap,
            header_max_len=header_max_len,
        )
        if verdict.valid:
            kept.append(r)
        else:
            dropped.append((r, verdict))
    return kept, dropped
