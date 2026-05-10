"""Stage 10 — render a per-run gap + conflict + low-confidence PDF report.

PyMuPDF is already a dependency (used by the chunker). We hand-draw a
simple multi-page report with text + colored bands rather than depend on
an HTML→PDF library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..database import SessionLocal
from ..models import (
    Conflict,
    Gap,
    Project,
    ScopeExtractionRun,
    ScopeItem,
)

log = logging.getLogger(__name__)


_PAGE_WIDTH = 612  # US Letter
_PAGE_HEIGHT = 792
_MARGIN_X = 54
_MARGIN_Y = 54

_COLOR_HEADING = (0.12, 0.16, 0.21)
_COLOR_SUBTLE = (0.45, 0.45, 0.50)
_COLOR_RED = (0.85, 0.20, 0.30)
_COLOR_AMBER = (0.90, 0.55, 0.10)
_COLOR_BLUE = (0.20, 0.45, 0.85)
_COLOR_GREEN = (0.18, 0.62, 0.40)


@dataclass
class GeneratedReport:
    file_path: Path
    file_size: int
    page_count: int


def _severity_color(sev: str) -> tuple[float, float, float]:
    if sev == "blocker":
        return _COLOR_RED
    if sev == "warn":
        return _COLOR_AMBER
    return _COLOR_BLUE


def _tier_color(tier: str | None) -> tuple[float, float, float]:
    if tier == "GREEN":
        return _COLOR_GREEN
    if tier == "YELLOW":
        return _COLOR_AMBER
    if tier == "RED":
        return _COLOR_RED
    return _COLOR_SUBTLE


class _Cursor:
    """Minimal layout helper that auto-paginates."""

    def __init__(self, doc: fitz.Document):
        self.doc = doc
        self.page = doc.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
        self.y = _MARGIN_Y

    def newpage(self) -> None:
        self.page = self.doc.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
        self.y = _MARGIN_Y

    def ensure(self, needed: float) -> None:
        if self.y + needed > _PAGE_HEIGHT - _MARGIN_Y:
            self.newpage()

    def write(
        self,
        text: str,
        *,
        size: float = 10,
        color: tuple[float, float, float] = (0.10, 0.10, 0.15),
        bold: bool = False,
        line_height: float = 1.35,
    ) -> None:
        self.ensure(size * line_height)
        font = "helv" if not bold else "hebo"
        wrapped = self.page.insert_textbox(
            fitz.Rect(_MARGIN_X, self.y, _PAGE_WIDTH - _MARGIN_X, _PAGE_HEIGHT - _MARGIN_Y),
            text,
            fontname=font,
            fontsize=size,
            color=color,
            align=0,
        )
        # insert_textbox returns negative when text overflowed; treat anything
        # negative as "needed more room" and just paginate. For our short
        # snippets this rarely fires.
        if wrapped < 0:
            # Paginate and try once more
            self.newpage()
            self.page.insert_textbox(
                fitz.Rect(
                    _MARGIN_X, self.y, _PAGE_WIDTH - _MARGIN_X, _PAGE_HEIGHT - _MARGIN_Y
                ),
                text,
                fontname=font,
                fontsize=size,
                color=color,
            )
        # Estimate consumed lines from text length (cheap heuristic)
        approx_lines = max(1, (len(text) // 95) + 1)
        self.y += size * line_height * approx_lines

    def hr(self) -> None:
        self.ensure(8)
        self.page.draw_line(
            fitz.Point(_MARGIN_X, self.y + 2),
            fitz.Point(_PAGE_WIDTH - _MARGIN_X, self.y + 2),
            color=(0.85, 0.85, 0.88),
            width=0.5,
        )
        self.y += 8

    def colorband(self, label: str, value: str, color: tuple[float, float, float]) -> None:
        self.ensure(22)
        self.page.draw_rect(
            fitz.Rect(_MARGIN_X, self.y, _PAGE_WIDTH - _MARGIN_X, self.y + 18),
            color=color,
            fill=color,
            overlay=True,
            fill_opacity=0.10,
        )
        self.page.insert_text(
            fitz.Point(_MARGIN_X + 6, self.y + 12),
            f"{label}: {value}",
            fontname="hebo",
            fontsize=9,
            color=color,
        )
        self.y += 22


async def write_gap_report(
    run_id: str, *, output_dir: Path
) -> GeneratedReport | None:
    output_dir.mkdir(parents=True, exist_ok=True)

    async with SessionLocal() as db:
        run = await db.get(ScopeExtractionRun, run_id)
        if run is None:
            return None
        project = await db.get(Project, run.project_id)
        if project is None:
            return None

        gaps = (
            await db.execute(
                select(Gap).where(Gap.run_id == run_id).order_by(Gap.severity, Gap.created_at)
            )
        ).scalars().all()
        conflicts = (
            await db.execute(
                select(Conflict)
                .options(selectinload(Conflict.members))
                .where(Conflict.run_id == run_id)
            )
        ).scalars().all()
        low_items = (
            await db.execute(
                select(ScopeItem)
                .where(ScopeItem.run_id == run_id)
                .where(ScopeItem.evidence_tier == "INFERRED_LOW_CONFIDENCE")
            )
        ).scalars().all()

    doc = fitz.open()
    cur = _Cursor(doc)

    # Cover
    cur.write(f"{project.name}", size=22, bold=True, color=_COLOR_HEADING)
    cur.write(
        f"Gap & Conflict Report  ·  Run {run.id[:8]}  ·  "
        f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        size=10,
        color=_COLOR_SUBTLE,
    )
    cur.y += 6
    cur.hr()

    if run.trust_score is not None:
        tier = (run.trust_score_components or {}).get("tier") or "—"
        cur.colorband(
            "Project trust score",
            f"{run.trust_score:.2f}  ({tier})",
            _tier_color(tier),
        )

    cur.colorband(
        "Conflicts (open)",
        str(sum(1 for c in conflicts if c.status == "open")),
        _COLOR_RED,
    )
    cur.colorband(
        "Gaps (blocker)",
        str(sum(1 for g in gaps if g.severity == "blocker")),
        _COLOR_RED,
    )
    cur.colorband(
        "Gaps (warn)",
        str(sum(1 for g in gaps if g.severity == "warn")),
        _COLOR_AMBER,
    )
    cur.colorband(
        "Low-confidence items",
        str(len(low_items)),
        _COLOR_RED,
    )

    # Conflicts
    cur.y += 10
    cur.write("Conflicts", size=14, bold=True, color=_COLOR_HEADING)
    cur.hr()
    if not conflicts:
        cur.write("No conflicts surfaced.", size=10, color=_COLOR_SUBTLE)
    else:
        for c in conflicts:
            cur.write(
                f"• {c.conflict_type.replace('_', ' ').upper()}  ·  "
                f"status={c.status}  ·  members={len(c.members)}",
                size=10,
                bold=True,
            )
            if c.csi_division:
                cur.write(f"   Division {c.csi_division}", size=9, color=_COLOR_SUBTLE)
            if c.arbitration_reasoning:
                cur.write(
                    f"   Resolution: {c.arbitration_reasoning[:200]}",
                    size=9,
                    color=_COLOR_SUBTLE,
                )

    # Gaps
    cur.y += 10
    cur.write("Gaps", size=14, bold=True, color=_COLOR_HEADING)
    cur.hr()
    if not gaps:
        cur.write("No gaps surfaced.", size=10, color=_COLOR_SUBTLE)
    else:
        for g in gaps[:200]:  # cap to keep PDF readable
            cur.write(
                f"[{g.severity.upper()}] {g.gap_type.replace('_', ' ')}",
                size=10,
                bold=True,
                color=_severity_color(g.severity),
            )
            cur.write(f"   {g.description[:280]}", size=9, color=_COLOR_HEADING)
            if g.suggested_remediation:
                cur.write(
                    f"   → {g.suggested_remediation[:200]}",
                    size=9,
                    color=_COLOR_SUBTLE,
                )

    # Low confidence summary
    cur.y += 10
    cur.write(
        "Low-confidence items (sample)", size=14, bold=True, color=_COLOR_HEADING
    )
    cur.hr()
    if not low_items:
        cur.write(
            "No items flagged INFERRED_LOW_CONFIDENCE.",
            size=10,
            color=_COLOR_SUBTLE,
        )
    else:
        for it in low_items[:60]:
            cur.write(
                f"{it.csi_code}  ·  conf {it.confidence:.0%}",
                size=10,
                bold=True,
            )
            cur.write(f"   {it.description[:280]}", size=9, color=_COLOR_HEADING)

    file_path = output_dir / "gap_report.pdf"
    doc.save(file_path, deflate=True)
    page_count = doc.page_count
    doc.close()

    return GeneratedReport(
        file_path=file_path,
        file_size=file_path.stat().st_size,
        page_count=page_count,
    )
