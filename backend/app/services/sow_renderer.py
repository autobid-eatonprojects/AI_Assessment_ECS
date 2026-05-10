"""SOW Renderer — produces the industry-standard 10-section construction
Scope of Work document from a completed ScopeExtractionRun.

The 10 sections (per Procore / Smartsheet / BuildBook industry templates):
  1. Project Information     — from ProjectProfile (owner, address, codes, …)
  2. Scope Summary           — auto-generated narrative from the run
  3. Included Work           — line items grouped by CSI division + section
  4. Exclusions              — GC-level boilerplate + per-bid exclusions ref
  5. Assumptions             — pricing-basis assumptions
  6. Materials & Specs       — citation index per division
  7. Schedule / Milestones   — placeholder (not yet modeled in the system)
  8. Submittals / Closeout   — admin-class items (`item_type=admin`)
  9. Coordination            — cross-trade gaps + cross-division overlaps
  10. Change-Order Process   — boilerplate (AIA A201-2017 reference)

Two public surfaces (the interface IS the test surface):

  - `render_sow_markdown(...)` — pure: takes pre-loaded items / packages /
    profile / exclusions / conflicts / gaps and returns the document string.
    No DB, no I/O. Tests construct synthetic input data and assert on output.

  - `render_sow_for_run(run_id)` — DB-bound: loads everything for a run id
    and calls the pure renderer. This is what the API endpoint or CLI
    script invokes.

Action-verb hygiene: scope-item descriptions written before the
section_extractor's prompt was updated (any `extraction_method` other
than `section_extractor/*` post-fix) typically don't start with an
industry-standard action verb. The renderer pre-pends one based on the
extraction_method tag so the rendered SOW reads as a real bid document.
Today's data: backward compatible. Tomorrow's data: the new prompt
produces the verb natively, so the prepend is a no-op.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import SessionLocal
from ..models import (
    BidExclusion,
    Conflict,
    Document,
    Gap,
    ProjectProfile,
    Project,
    ScopeCitation,
    ScopeExtractionRun,
    ScopeItem,
    TradePackage,
)

log = logging.getLogger(__name__)


# ============================================================================
# Action-verb hygiene
# ============================================================================

_ACTION_VERBS = (
    "furnish", "install", "provide", "remove", "dispose", "demolish",
    "construct", "fabricate", "test", "certify", "submit", "verify",
    "supply", "perform", "execute", "deliver", "erect",
)

_VERB_PATTERN = re.compile(
    r"^\s*(?:" + "|".join(_ACTION_VERBS) + r")\b",
    re.IGNORECASE,
)


def _starts_with_action_verb(description: str) -> bool:
    return bool(_VERB_PATTERN.match(description or ""))


def _verb_for_item_type(extraction_method: str | None) -> str:
    """Return the industry-standard action verb to prepend, based on the
    extraction_method tag (`section_extractor/material` etc.)."""
    method = (extraction_method or "").lower()
    if "/material" in method:
        return "Furnish and install"
    if "/equipment" in method:
        return "Furnish, install, and connect"
    if "/admin" in method:
        return "Provide"
    if "/demo" in method:
        return "Remove and dispose of"
    if "/qc" in method:
        return "Test and certify"
    # discipline_agent_v1 / schedule_miner / unknown — best-effort default
    return "Furnish and install"


def _ensure_action_verb(item: ScopeItem) -> str:
    """Return the item description with an industry-standard action verb.

    The first letter of the original description gets lowercased ONLY when
    it's a normal Title-Case start (e.g. "Gypsum board" → "gypsum board"),
    so the result reads as one sentence. Acronyms (AHU-1, MEP, RCP), tags
    starting with digits/symbols, and already-lowercase starts are left
    alone.
    """
    desc = (item.description or "").strip()
    if not desc:
        return desc
    if _starts_with_action_verb(desc):
        return desc
    verb = _verb_for_item_type(item.extraction_method)
    first = desc[0]
    # Determine if the original starts with sentence-case (single uppercase
    # then lowercase) — that's the only case where lowercasing reads better.
    second = desc[1] if len(desc) > 1 else ""
    is_sentence_case = first.isupper() and second.islower()
    if is_sentence_case:
        return f"{verb} {first.lower()}{desc[1:]}"
    # Acronyms / lowercase / digit / symbol — leave as-is.
    return f"{verb} {desc}"


# ============================================================================
# Public surface 1 — pure markdown rendering
# ============================================================================


@dataclass
class SOWInput:
    """Everything the renderer needs. Built once by render_sow_for_run()."""
    run: ScopeExtractionRun
    project: Project
    profile: ProjectProfile | None
    items: list[ScopeItem]
    packages: list[TradePackage]
    exclusions: list[BidExclusion]
    gaps: list[Gap]
    conflicts: list[Conflict]
    division_titles: dict[str, str] = field(default_factory=dict)
    section_titles: dict[str, str] = field(default_factory=dict)
    citations_by_item: dict[str, list[ScopeCitation]] = field(default_factory=dict)
    documents_by_id: dict[str, Document] = field(default_factory=dict)


def render_sow_markdown(data: SOWInput) -> str:
    """Produce the 10-section industry-standard SOW as Markdown.

    Pure function. No DB, no I/O. Test by constructing a SOWInput in
    memory and asserting on the returned string.
    """
    parts: list[str] = []
    parts.append(_section_1_project_info(data))
    parts.append(_section_2_scope_summary(data))
    parts.append(_section_3_included_work(data))
    parts.append(_section_4_exclusions(data))
    parts.append(_section_5_assumptions(data))
    parts.append(_section_6_materials_and_specs(data))
    parts.append(_section_7_schedule(data))
    parts.append(_section_8_submittals_closeout(data))
    parts.append(_section_9_coordination(data))
    parts.append(_section_10_change_orders(data))
    return "\n\n".join(p.strip() for p in parts if p) + "\n"


# ----- Section 1: Project Information -----


def _section_1_project_info(data: SOWInput) -> str:
    p = data.project
    profile = data.profile
    rows: list[tuple[str, str]] = []
    rows.append(("Project name", p.name or "—"))
    rows.append(("Project number", (profile and profile.project_number) or "—"))
    rows.append(("Location", (profile and profile.location) or "—"))
    rows.append(("Building type", (profile and profile.building_type) or "—"))
    rows.append((
        "Size",
        f"{profile.size_sf:,.0f} SF" if profile and profile.size_sf else "—",
    ))
    rows.append(("Stories", str(profile.stories) if profile and profile.stories is not None else "—"))
    rows.append(("Construction type", (profile and profile.construction_type) or "—"))
    rows.append(("Occupancy", (profile and profile.occupancy) or "—"))
    rows.append((
        "Sprinklered",
        "Yes" if (profile and profile.sprinklered) else
        "No" if (profile and profile.sprinklered is False) else "—",
    ))
    if profile and profile.codes:
        rows.append(("Applicable codes", ", ".join(profile.codes)))
    rows.append((
        "Run id",
        f"`{data.run.id}` — {data.run.completed_at or data.run.started_at}",
    ))
    body = "\n".join(f"- **{k}:** {v}" for k, v in rows)
    return f"# Scope of Work\n\n## 1. Project Information\n\n{body}"


# ----- Section 2: Scope Summary -----


def _section_2_scope_summary(data: SOWInput) -> str:
    div_count = len({i.csi_division for i in data.items if i.csi_division})
    item_count = len(data.items)
    pkg_count = len(data.packages)
    drawing_cited = sum(
        1
        for cits in data.citations_by_item.values()
        if any((c.evidence_type or "").lower() == "drawing" for c in cits)
    )
    spec_cited = sum(
        1
        for cits in data.citations_by_item.values()
        if any((c.evidence_type or "").lower() == "spec" for c in cits)
    )
    bilateral = sum(
        1
        for cits in data.citations_by_item.values()
        if {"drawing", "spec"}.issubset(
            {(c.evidence_type or "").lower() for c in cits}
        )
    )
    body = (
        f"This project comprises **{item_count:,} biddable scope items** "
        f"across **{div_count} CSI MasterFormat divisions**, organized into "
        f"**{pkg_count} trade packages** for sub-bidder solicitation.\n\n"
        f"Of the {item_count} items, {drawing_cited:,} carry drawing-side "
        f"evidence, {spec_cited:,} carry spec-side evidence, and **{bilateral:,} "
        f"carry bilateral evidence (both drawing and spec)** — the standard "
        f"of grounded extraction enforced by the L2 citation validator and "
        f"per-citation entailment judge."
    )
    return f"## 2. Scope Summary\n\n{body}"


# ----- Section 3: Included Work -----


def _section_3_included_work(data: SOWInput) -> str:
    """Items grouped by csi_division, then by csi_code (section), then listed."""
    by_div: dict[str, list[ScopeItem]] = defaultdict(list)
    for it in data.items:
        # Skip admin/QC items here — they go in section 8
        if (it.extraction_method or "").lower().split("/")[-1] in ("admin", "qc"):
            continue
        by_div[it.csi_division or "??"].append(it)

    if not by_div:
        return "## 3. Included Work\n\n_(no biddable items)_"

    out: list[str] = ["## 3. Included Work\n"]
    for div in sorted(by_div):
        title = data.division_titles.get(div) or f"Division {div}"
        out.append(f"### {title}")
        # Group by section within division
        by_section: dict[str, list[ScopeItem]] = defaultdict(list)
        for it in by_div[div]:
            by_section[it.csi_code or "??"].append(it)
        for section_code in sorted(by_section):
            section_title = (
                data.section_titles.get(section_code) or f"Section {section_code}"
            )
            out.append(f"\n**{section_code} — {section_title}**\n")
            for it in by_section[section_code]:
                out.append(_format_item_line(it, data))
        out.append("")
    return "\n".join(out)


def _format_item_line(item: ScopeItem, data: SOWInput) -> str:
    """One bullet line: action-verb + qty/unit + description + drawing + spec refs."""
    desc = _ensure_action_verb(item)
    qty_unit = ""
    if item.quantity and item.unit:
        qty_unit = f" ({item.quantity} {item.unit})"
    elif item.unit:
        qty_unit = f" ({item.unit})"

    # Citation refs — grab unique sheet numbers + docs from citations
    cits = data.citations_by_item.get(item.id, [])
    sheet_refs = sorted({
        c.sheet_number for c in cits
        if c.sheet_number and (c.evidence_type or "").lower() == "drawing"
    })
    has_spec = any((c.evidence_type or "").lower() == "spec" for c in cits)
    refs: list[str] = []
    if sheet_refs:
        refs.append(f"per {', '.join(sheet_refs)}")
    if has_spec and item.csi_code:
        refs.append(f"per spec section {item.csi_code}")
    refs_str = (" " + "; ".join(refs)) if refs else ""

    return f"- {desc}{qty_unit}{refs_str}."


# ----- Section 4: Exclusions -----


def _section_4_exclusions(data: SOWInput) -> str:
    body = (
        "Exclusions at the GC project level:\n\n"
        "- Owner-furnished, owner-installed (OFOI) items unless explicitly listed.\n"
        "- Furniture, fixtures, and equipment (FF&E) outside the construction contract.\n"
        "- Permits and fees not obtained directly by the GC.\n"
        "- Work outside the property line unless shown on civil drawings.\n"
    )
    if data.exclusions:
        body += (
            f"\n\nPer individual subcontractor bids, **{len(data.exclusions)} additional "
            "exclusions** are tracked and surfaced in the bid-leveling pivot. "
            "Refer to the Bid Analysis page for per-vendor exclusion lists."
        )
    return f"## 4. Exclusions\n\n{body}"


# ----- Section 5: Assumptions -----


def _section_5_assumptions(data: SOWInput) -> str:
    body = (
        "Pricing basis:\n\n"
        "- Drawings and specifications as issued for bid; addenda incorporated.\n"
        "- Standard-hour weekday work; no overtime, no shift premiums.\n"
        "- Material pricing valid 30 days from bid date.\n"
        "- Single mobilization unless schedule dictates phased execution.\n"
        "- Existing field conditions match what's shown on documents; differing "
        "site conditions handled per AIA A201-2017 §3.7.4.\n"
        "- All scope quantities verified against drawings during the bid period; "
        "any quantity discrepancy is flagged via RFI before bid close.\n"
    )
    return f"## 5. Assumptions\n\n{body}"


# ----- Section 6: Materials & Specifications -----


def _section_6_materials_and_specs(data: SOWInput) -> str:
    """List unique CSI sections cited, with a count of items per section."""
    section_counts: dict[str, int] = defaultdict(int)
    for it in data.items:
        if it.csi_code:
            section_counts[it.csi_code] += 1
    if not section_counts:
        return "## 6. Materials & Specifications\n\n_(no spec citations)_"
    lines = ["Sections referenced in this scope:\n"]
    for code in sorted(section_counts):
        title = data.section_titles.get(code) or "(untitled)"
        n = section_counts[code]
        lines.append(f"- **{code}** — {title} _(used by {n} item{'s' if n != 1 else ''})_")
    return "## 6. Materials & Specifications\n\n" + "\n".join(lines)


# ----- Section 7: Schedule -----

# Schedule-related Division 01 sections — when these are present in the
# project, surface them as the authoritative schedule references rather
# than handing the reader an empty section.
_SCHEDULE_REFERENCE_SECTIONS = {
    "01 11 00": "Summary of Work",
    "01 12 00": "Multiple Contract Summary",
    "01 14 00": "Work Restrictions",
    "01 32 16": "Construction Progress Schedule",
    "01 32 33": "Photographic Documentation",
    "01 32 50": "Construction Progress Documentation",
    "01 33 00": "Submittal Procedures",
    "01 35 00": "Special Project Procedures",
    "01 73 00": "Execution",
    "01 77 00": "Closeout Procedures",
}


def _section_7_schedule(data: SOWInput) -> str:
    """Schedule & Milestones — references the spec sections that govern
    sequencing and timing rather than enumerating dates the AI can't
    derive from the manual alone.

    Estimating teams expect this section to point at the contractually
    binding schedule sources (Construction Progress Schedule, Work
    Restrictions, Submittal Procedures, Closeout) — not invented dates.
    Pricing here assumes those documents are followed; the SOW just
    indexes them so the reader can find them in seconds.
    """
    out = ["## 7. Schedule and Milestones\n"]

    # Sequencing constraints derived from CSI Division 01 sections in the project
    found = [
        (code, _SCHEDULE_REFERENCE_SECTIONS[code])
        for code in _SCHEDULE_REFERENCE_SECTIONS
        if code in data.section_titles
    ]

    out.append(
        "Schedule and milestone dates are project-specific and live on "
        "the GC's CPM schedule rather than in the spec book. The spec "
        "sections below govern sequencing, work restrictions, and "
        "closeout timing; this SOW's pricing assumes they are followed.\n"
    )

    if found:
        out.append("**Schedule-governing spec sections in this project:**\n")
        for code, default_title in found:
            actual_title = data.section_titles.get(code) or default_title
            out.append(f"- **{code}** — {actual_title}")
        out.append("")

    # Surface known sequencing dependencies from Phase 9 cross-trade
    # gaps (predecessor/successor relationships flagged during gap
    # detection). When a gap pertains to sequencing rather than missing
    # work, include it here so the bidder sees the dependency.
    sequencing_gaps = [
        g for g in data.gaps
        if g.severity in ("blocker", "warn")
        and any(
            kw in (g.description or "").lower()
            for kw in ("before", "after", "prior to", "sequenc", "coordinate", "phase")
        )
    ]
    if sequencing_gaps:
        out.append("**Sequencing dependencies surfaced during scope review:**\n")
        for g in sequencing_gaps[:8]:
            out.append(f"- {g.description}")
        if len(sequencing_gaps) > 8:
            out.append(f"- _…and {len(sequencing_gaps) - 8} more (see review queue)._")
        out.append("")

    out.append(
        "_Definitive milestone dates (mobilization, substantial completion, "
        "final completion) come from the GC's CPM schedule and any "
        "addenda. Bidders should confirm these dates against the latest "
        "schedule revision before submitting._"
    )
    return "\n".join(out)


# ----- Section 8: Submittals & Closeout -----


def _section_8_submittals_closeout(data: SOWInput) -> str:
    """Admin-class items, grouped by section."""
    admin_items = [
        it for it in data.items
        if (it.extraction_method or "").lower().split("/")[-1] in ("admin", "qc")
    ]
    if not admin_items:
        return "## 8. Submittals, Inspections, and Closeout\n\n_(no admin scope extracted)_"

    by_section: dict[str, list[ScopeItem]] = defaultdict(list)
    for it in admin_items:
        by_section[it.csi_code or "??"].append(it)

    out = ["## 8. Submittals, Inspections, and Closeout\n"]
    out.append(
        f"_{len(admin_items)} administrative scope items extracted across "
        f"{len(by_section)} sections._\n"
    )
    for section_code in sorted(by_section):
        section_title = data.section_titles.get(section_code) or "(untitled)"
        out.append(f"### {section_code} — {section_title}\n")
        for it in by_section[section_code]:
            out.append(_format_item_line(it, data))
        out.append("")
    return "\n".join(out)


# ----- Section 9: Coordination -----


def _section_9_coordination(data: SOWInput) -> str:
    cross_div_conflicts = [
        c for c in data.conflicts
        if c.conflict_type == "cross_division_overlap" and c.status == "open"
    ]
    cross_ref_gaps = [g for g in data.gaps if g.gap_type == "unresolved_cross_reference"]
    body = (
        "Inter-trade coordination requirements (auto-surfaced from drawing "
        "cross-references and CSI division overlaps):\n"
    )
    if cross_div_conflicts:
        body += (
            f"\n- **{len(cross_div_conflicts)} unresolved cross-division overlaps** "
            "where the same physical scope appears in two CSI divisions "
            "(e.g. sealed concrete in both Div 03 and Div 09). These require "
            "explicit assignment in the GC's bid leveling pivot.\n"
        )
    if cross_ref_gaps:
        body += (
            f"\n- **{len(cross_ref_gaps)} unresolved drawing cross-references** "
            "where one drawing points to a sheet detail not extracted in scope. "
            "Verify each reference during bid review (typical pattern: "
            "structural detail sheets like S2.1, A5.3 wall-section detail bubbles).\n"
        )
    if not cross_div_conflicts and not cross_ref_gaps:
        body += "\n- No outstanding inter-trade coordination items.\n"
    return f"## 9. Coordination\n\n{body}"


# ----- Section 10: Change-Order Process -----


def _section_10_change_orders(data: SOWInput) -> str:
    return (
        "## 10. Change-Order Process\n\n"
        "Changes to the scope after contract execution are processed per "
        "**AIA Document A201-2017** Article 7 (Changes in the Work):\n\n"
        "- Owner-initiated changes: written Change Order signed by Owner, "
        "Architect, and Contractor.\n"
        "- Construction Change Directive (CCD) for changes the Owner wants "
        "executed before pricing is finalized; converted to a Change Order "
        "after price agreement.\n"
        "- Minor changes consistent with the Contract Documents and not "
        "involving cost or time adjustment: written order from the Architect.\n"
        "- Pricing methodology: unit prices in the bid form take precedence; "
        "otherwise mutual lump-sum agreement OR cost plus fee per Section "
        "01 26 00.\n"
    )


# ============================================================================
# Public surface 2 — DB-bound: load and render for a run
# ============================================================================


async def render_sow_for_run(run_id: str) -> str:
    """Load all SOW data for a completed run, render markdown, return string."""
    data = await _load_sow_input(run_id)
    if data is None:
        return f"# Scope of Work\n\n_(run {run_id} not found)_\n"
    return render_sow_markdown(data)


async def _load_sow_input(run_id: str) -> SOWInput | None:
    async with SessionLocal() as db:
        run = await db.get(ScopeExtractionRun, run_id)
        if run is None:
            return None
        project = await db.get(Project, run.project_id)
        if project is None:
            return None
        profile = (
            await db.execute(
                select(ProjectProfile).where(
                    ProjectProfile.project_id == project.id
                )
            )
        ).scalar_one_or_none()
        items = list(
            (
                await db.execute(
                    select(ScopeItem).where(ScopeItem.run_id == run_id)
                )
            ).scalars().all()
        )
        packages = list(
            (
                await db.execute(
                    select(TradePackage).where(TradePackage.run_id == run_id)
                )
            ).scalars().all()
        )
        exclusions = list(
            (
                await db.execute(
                    select(BidExclusion).where(
                        BidExclusion.project_id == project.id
                    )
                )
            ).scalars().all()
        )
        gaps = list(
            (
                await db.execute(
                    select(Gap).where(Gap.run_id == run_id)
                )
            ).scalars().all()
        )
        conflicts = list(
            (
                await db.execute(
                    select(Conflict).where(Conflict.run_id == run_id)
                )
            ).scalars().all()
        )
        # Build citation index
        item_ids = [it.id for it in items]
        cit_rows: list[ScopeCitation] = []
        if item_ids:
            cit_rows = list(
                (
                    await db.execute(
                        select(ScopeCitation).where(
                            ScopeCitation.scope_item_id.in_(item_ids)
                        )
                    )
                ).scalars().all()
            )
        citations_by_item: dict[str, list[ScopeCitation]] = defaultdict(list)
        for c in cit_rows:
            citations_by_item[c.scope_item_id].append(c)

        # Documents — used for ref text in citations
        doc_ids = {c.document_id for c in cit_rows if c.document_id}
        documents_by_id: dict[str, Document] = {}
        if doc_ids:
            documents_by_id = {
                d.id: d
                for d in (
                    await db.execute(
                        select(Document).where(Document.id.in_(doc_ids))
                    )
                ).scalars().all()
            }

    # Division + section title indices from items themselves
    division_titles = {
        it.csi_division: (it.division_label or it.csi_division)
        for it in items
        if it.csi_division
    }
    section_titles: dict[str, str] = {}
    for it in items:
        if it.csi_code and it.section_title and it.csi_code not in section_titles:
            section_titles[it.csi_code] = it.section_title

    return SOWInput(
        run=run,
        project=project,
        profile=profile,
        items=items,
        packages=packages,
        exclusions=exclusions,
        gaps=gaps,
        conflicts=conflicts,
        division_titles=division_titles,
        section_titles=section_titles,
        citations_by_item=dict(citations_by_item),
        documents_by_id=documents_by_id,
    )
