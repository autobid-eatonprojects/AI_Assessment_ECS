"""P2 — Spec TOC reconstruction → project-specific CSI subset.

The plan PDF observes that scanned spec books have NO embedded PDF
outline (~370 pages, image-only). The TOC must be reconstructed from
the text after OCR. We then reconcile what we found against the
canonical CSI MasterFormat vocabulary (loaded from Trade_List.xlsx) to
produce the project-specific CSI subset — every section that's actually
referenced in this project's spec.

Why this matters:
  - Shrinks downstream agent context ~10× — instead of carrying all
    ~1,250 generic CSI sections, the discipline agents (P3) only see
    the ~150-200 sections this project's spec actually covers
  - Cross-checks per-page extraction: if PART 1 of section 23 21 13
    appeared but PART 2 didn't, flag a possible OCR / parse failure
  - Feeds the trust score's spec_section_coverage component (20%):
    items_in_extracted_subset / items_extractor_actually_emitted

Approach:
  1. Pull every page_text chunk for the project's written-spec docs
  2. Regex-detect "SECTION XX XX XX — TITLE" headers
  3. Reconcile each detected section against the CSI taxonomy
  4. Sonnet pass over the reconstructed TOC + ambiguous matches to
     confirm + fill gaps in numerical sequence

Cost: ~$0.05-0.10 per project (one Sonnet call over the section list).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select

from ..database import SessionLocal
from ..models import Chunk, Document
from .anthropic_tool_call import _get_client, call_with_tool
from .trade_list_parser import get_taxonomy_for_project

log = logging.getLogger(__name__)


# Match "SECTION XX XX XX — TITLE" or "Section XX XX XX - Title" anywhere
# on a line. CSI sections are 6 digits in 2-2-2 grouping.
_SECTION_RE = re.compile(
    r"\b(?:SECTION|Section)\s+(\d{2}\s\d{2}\s\d{2})\s*[—–\-:]?\s*([A-Z][A-Z0-9\s,&/\-'.]+)?",
    re.MULTILINE,
)


@dataclass
class _DetectedSection:
    csi_code: str
    title_raw: str | None
    occurrences: int  # how many times this section header appeared in spec text


_CONFIRM_TOOL = {
    "name": "confirm_csi_subset",
    "description": (
        "Given the regex-detected sections from the project's spec book, "
        "the canonical CSI MasterFormat vocabulary, and any sequence "
        "gaps that look suspicious, return the FINAL project-specific "
        "CSI subset that should drive downstream extraction. Confirm "
        "real sections, drop misreads (regex caught a number in body "
        "text), and add sections likely missed (e.g., PART 2 PRODUCTS "
        "but no PART 3 EXECUTION usually means we missed PART 3)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "confirmed_sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "csi_code": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "confidence": {
                            "type": "string",
                            "enum": ["confirmed", "inferred", "uncertain"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["csi_code", "confidence"],
                },
            },
            "rejected_misreads": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "csi_code": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["csi_code"],
                },
            },
        },
        "required": ["confirmed_sections"],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT = """\
You are reconciling a regex-detected list of CSI sections from a \
construction spec book against the canonical CSI MasterFormat \
vocabulary. Output the FINAL project-specific CSI subset.

For each detected section:
  - confirmed: section_id is in the canonical vocab AND appears
    multiple times in spec text (low chance of being a misread).
  - inferred: section_id is in the canonical vocab BUT only appeared
    once or appeared in suspicious context (book index, callout in
    drawing); still likely real.
  - uncertain: appeared but couldn't confirm; default to keeping it
    unless the title is wildly mismatched.

For sections likely MISSED (numerical gaps in the detected sequence
that the canonical vocab knows about — e.g., we saw 03 30 00 + 03 35 00
+ 03 40 00 but missed 03 33 00 which exists in the canonical and is a
common section), emit them with confidence=inferred and a reason
explaining the inference.

For misreads (e.g., "07 00 00" detected from a body-text page number
not a section header), put them in rejected_misreads with a brief reason.

Use confirm_csi_subset now.
"""


async def _detect_in_text(project_id: str) -> list[_DetectedSection]:
    """Pull spec page_text chunks and run the SECTION-header regex."""
    async with SessionLocal() as db:
        chunks = (
            await db.execute(
                select(Chunk.text)
                .join(Document, Chunk.document_id == Document.id)
                .where(Chunk.project_id == project_id)
                .where(Document.doc_type == "written-spec")
                .where(Chunk.chunk_type.in_(["page_text", "page_summary"]))
            )
        ).all()
    counts: dict[str, _DetectedSection] = {}
    for (text,) in chunks:
        if not text:
            continue
        for m in _SECTION_RE.finditer(text):
            csi = m.group(1)
            title = (m.group(2) or "").strip().rstrip(".:") or None
            if csi in counts:
                counts[csi].occurrences += 1
                if title and not counts[csi].title_raw:
                    counts[csi].title_raw = title
            else:
                counts[csi] = _DetectedSection(
                    csi_code=csi, title_raw=title, occurrences=1
                )
    return list(counts.values())


async def reconstruct_for_project(project_id: str) -> dict:
    """End-to-end: detect → reconcile → return project CSI subset.

    Returns {"detected_count", "confirmed", "rejected", "cost_usd"}.
    The confirmed list IS the project-specific CSI subset; downstream
    consumers (P3 discipline agents) read this from `project_csi_subset`
    JSON on ScopeExtractionRun.config when it lands.
    """
    detected = await _detect_in_text(project_id)
    if not detected:
        log.info(
            "spec_toc: no SECTION headers detected for %s "
            "(no written-spec docs uploaded?)",
            project_id,
        )
        return {
            "detected_count": 0,
            "confirmed": [],
            "rejected": [],
            "cost_usd": 0.0,
        }

    taxonomy = await get_taxonomy_for_project(project_id)
    canonical_codes = sorted(taxonomy.section_codes) if taxonomy else []

    # If we have no canonical vocab, fall back to detected-only
    if not canonical_codes:
        return {
            "detected_count": len(detected),
            "confirmed": [
                {"csi_code": d.csi_code, "title": d.title_raw,
                 "confidence": "uncertain", "reason": "no canonical vocab"}
                for d in detected
            ],
            "rejected": [],
            "cost_usd": 0.0,
        }

    client = _get_client()
    if client is None:
        # No LLM — return regex hits intersected with canonical
        canonical_set = set(canonical_codes)
        return {
            "detected_count": len(detected),
            "confirmed": [
                {"csi_code": d.csi_code, "title": d.title_raw,
                 "confidence": "confirmed",
                 "reason": "regex match + in canonical (no LLM reconcile)"}
                for d in detected
                if d.csi_code in canonical_set
            ],
            "rejected": [
                {"csi_code": d.csi_code,
                 "reason": "regex match but not in canonical vocab"}
                for d in detected
                if d.csi_code not in canonical_set
            ],
            "cost_usd": 0.0,
        }

    # Build the prompt. Cap canonical list at 1500 rows to fit; the
    # taxonomy loader caps at ~1,250 generic sections so this is fine.
    prompt = (
        "DETECTED SECTIONS (from regex over OCR'd spec text):\n"
        + "\n".join(
            f"  {d.csi_code}: {(d.title_raw or '?')} ({d.occurrences}x)"
            for d in sorted(detected, key=lambda x: x.csi_code)
        )
        + "\n\nCANONICAL CSI MasterFormat (pulled from this project's "
        "Trade_List.xlsx):\n"
        + "\n".join(f"  {c}" for c in canonical_codes)
        + "\n\nReconcile and return confirm_csi_subset."
    )

    result = await call_with_tool(
        model="claude-sonnet-4-6",
        tool_def=_CONFIRM_TOOL,
        system=_SYSTEM_PROMPT,
        max_tokens=8192,
        purpose="spec-toc",
        project_id=project_id,
        user_content=prompt,
    )
    payload = result.parsed_input

    log.info(
        "spec_toc: project %s — %d detected, %d confirmed, $%.4f",
        project_id,
        len(detected),
        len(payload.get("confirmed_sections") or []),
        result.cost_usd,
    )
    return {
        "detected_count": len(detected),
        "confirmed": payload.get("confirmed_sections") or [],
        "rejected": payload.get("rejected_misreads") or [],
        "cost_usd": result.cost_usd,
    }
