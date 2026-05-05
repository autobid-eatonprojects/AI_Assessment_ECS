"""Parse an uploaded `Trade_List.xlsx` (CSI MasterFormat) into a structured taxonomy.

The supplied file (and CSI MasterFormat in general) has two row types:

    Division-header row:
        CSI Code  | Division                                     | Subdivision Description
        0         | 00 - Procurement & Contracting Requirements  | (blank)
        3         | 03 - Concrete                                | (blank)
        ...

    Section row:
        00 11 00  | (blank)                                      | Advertisements and Invitations
        03 30 00  | (blank)                                      | Cast-in-Place Concrete
        ...

We walk the rows, group sections under the most-recent division header, and
return a `CSITaxonomy` that downstream services use as the source-of-truth for
CSI-code grounding (Phase 4.3 Stage C).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import openpyxl


@dataclass(frozen=True)
class CSISection:
    code: str  # "03 30 00"
    title: str  # "Cast-in-Place Concrete"
    division_code: str  # "03"


@dataclass
class CSIDivision:
    code: str  # "03"
    label: str  # "03 - Concrete"
    sections: list[CSISection] = field(default_factory=list)


@dataclass
class CSITaxonomy:
    divisions: list[CSIDivision]

    @property
    def all_sections(self) -> list[CSISection]:
        return [s for d in self.divisions for s in d.sections]

    @property
    def section_codes(self) -> set[str]:
        return {s.code for s in self.all_sections}

    @property
    def division_codes(self) -> set[str]:
        return {d.code for d in self.divisions}

    def get_section(self, code: str) -> CSISection | None:
        for s in self.all_sections:
            if s.code == code:
                return s
        return None

    def get_division(self, code: str) -> CSIDivision | None:
        for d in self.divisions:
            if d.code == code:
                return d
        return None

    def fuzzy_lookup_section(
        self, proposed_code: str, top_k: int = 3
    ) -> list[tuple[CSISection, float]]:
        """Return up-to-top_k closest sections by string similarity.

        Used by the CSI grounder when Claude hallucinates a code that isn't in
        the canonical list — we'd rather snap to the nearest real code than
        accept a fabricated one.
        """
        results: list[tuple[CSISection, float]] = []
        for s in self.all_sections:
            score = SequenceMatcher(None, proposed_code, s.code).ratio()
            results.append((s, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


# Patterns for distinguishing division-header rows from section rows.
_DIVISION_LABEL_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*(.+)$")
_SECTION_CODE_RE = re.compile(r"^\s*(\d{2})\s+(\d{2})\s+(\d{2})\s*$")


def _parse_division_label(value) -> tuple[str, str] | None:
    """Returns (division_code_2digit, label) or None."""
    if not value:
        return None
    s = str(value).strip()
    m = _DIVISION_LABEL_RE.match(s)
    if not m:
        return None
    div_num = int(m.group(1))
    return f"{div_num:02d}", s


def _parse_section_code(value) -> tuple[str, str] | None:
    """Returns (canonical_section_code, division_code_2digit) or None."""
    if not value:
        return None
    s = str(value).strip()
    m = _SECTION_CODE_RE.match(s)
    if not m:
        return None
    canonical = f"{m.group(1)} {m.group(2)} {m.group(3)}"
    return canonical, m.group(1)


def parse_trade_list(path: Path) -> CSITaxonomy:
    """Parse the supplied .xlsx into a CSITaxonomy."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheet = wb.active

    divisions_by_code: dict[str, CSIDivision] = {}
    current_div: CSIDivision | None = None

    for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        if row is None:
            continue
        # Skip header
        if row_idx == 1:
            continue
        # Pad row to at least 3 cells
        cells = list(row) + [None] * max(0, 3 - len(row))
        code_cell, division_cell, subdiv_cell = cells[0], cells[1], cells[2]

        # Division-header rows: division_cell has "NN - Title"
        div_match = _parse_division_label(division_cell)
        if div_match:
            div_code, div_label = div_match
            if div_code not in divisions_by_code:
                divisions_by_code[div_code] = CSIDivision(code=div_code, label=div_label)
            current_div = divisions_by_code[div_code]
            continue

        # Section rows: code_cell has "NN NN NN", description in subdiv_cell
        section_match = _parse_section_code(code_cell)
        if section_match:
            canonical, div_code = section_match
            title = (str(subdiv_cell).strip() if subdiv_cell else "") or "(no title)"
            div = divisions_by_code.get(div_code)
            if div is None:
                # Section came before its header — synthesize a placeholder
                div = CSIDivision(code=div_code, label=f"{div_code} - (untitled)")
                divisions_by_code[div_code] = div
                current_div = div
            div.sections.append(
                CSISection(code=canonical, title=title, division_code=div_code)
            )
            continue

        # Anything else is junk — skip
        _ = current_div  # quiet unused-var lint when no section follows

    wb.close()
    # Sort divisions by code
    return CSITaxonomy(
        divisions=sorted(divisions_by_code.values(), key=lambda d: d.code)
    )


# -----------------------------------------------------------------------------
# DB-aware helpers
# -----------------------------------------------------------------------------


async def get_taxonomy_for_project(project_id: str) -> CSITaxonomy | None:
    """Look up the project's uploaded trade-list document and parse it.

    Returns None if no doc classified as `trade-list` is uploaded for the
    project — Phase 4.2 / 4.3 should treat that as a precondition failure.
    """
    from sqlalchemy import select

    from ..database import SessionLocal
    from ..models import Document
    from .storage import storage

    async with SessionLocal() as db:
        result = await db.execute(
            select(Document)
            .where(Document.project_id == project_id)
            .where(Document.doc_type == "trade-list")
            .order_by(Document.created_at.desc())
        )
        doc = result.scalars().first()
    if doc is None:
        return None
    path = storage.absolute_path(doc.storage_path)
    if not path.exists() or path.suffix.lower() not in (".xlsx", ".xlsm"):
        return None
    return parse_trade_list(path)
