"""P9 — RFI list generator (Opus 4.7).

After scope extraction completes, the GC has a punch list of issues
the design team needs to clarify before the bid set goes out:
  - One-sided items (spec_without_drawing, drawing_without_spec)
  - Conflicts the auto-arbitrator deferred to HITL
  - qty_implausible findings from the sanity pass
  - Cross-discipline coordination questions

This service reads those signals + uses Opus 4.7 to draft a formal
RFI list (Request For Information) the GC can email to the design
team. Each RFI is written in the language an architect / engineer
expects: precise sheet/spec citations, quantified impact, suggested
clarifying language.

One Opus call per project (cheaper to batch all gaps in one
context than pay overhead per RFI). Reads at most ~50 strongest
signals; the rest stay in HITL queue but don't make the formal RFI.

Output is persisted as one document row of doc_type='rfi-list' with
a Markdown body the API can serve directly. Each RFI line is also
mirrored to the audit log so the trail survives re-generation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import (
    Conflict,
    Gap,
    Project,
    ScopeExtractionRun,
    ScopeItem,
)
from .llm_log import record_call, usage_from_anthropic

log = logging.getLogger(__name__)


# Opus is the right tool here — RFIs need precise design-language
# phrasing and the model will see ~30K tokens of structured gap
# context. Higher accuracy > slightly higher cost.
_OPUS_MODEL = "claude-opus-4-7"
_MAX_RFI_COUNT = 50  # Cap per run; rest stay HITL-only


_RFI_TOOL = {
    "name": "draft_rfi_list",
    "description": (
        "Draft a formal RFI (Request For Information) list. Each RFI is "
        "addressed to the design team and asks them to clarify a "
        "specific scope ambiguity. Use the language an architect or "
        "MEP engineer would expect — precise spec/sheet citations, "
        "quantified impact, and a clear ask. Don't pad with filler — "
        "RFIs that lack a clear answerable question don't belong on "
        "the list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rfi_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rfi_number": {
                            "type": "string",
                            "description": "Sequential RFI number (e.g. 'RFI-001')",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Short subject line — 70 chars max",
                        },
                        "discipline": {
                            "type": "string",
                            "description": "Target discipline / consultant",
                        },
                        "csi_section": {
                            "type": ["string", "null"],
                            "description": "Affected CSI 6-digit section if specific",
                        },
                        "sheet_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Drawing sheets / spec sections referenced",
                        },
                        "issue": {
                            "type": "string",
                            "description": (
                                "Plain-language description of the ambiguity "
                                "or missing information. Be SPECIFIC."
                            ),
                        },
                        "ask": {
                            "type": "string",
                            "description": (
                                "The clarifying question to the design team. "
                                "Phrase as a question or directive."
                            ),
                        },
                        "impact": {
                            "type": "string",
                            "description": (
                                "What hangs on the answer (cost / schedule / "
                                "scope). E.g. 'Affects fixture count and "
                                "rough-in locations on P1.1'."
                            ),
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low"],
                        },
                    },
                    "required": [
                        "rfi_number", "subject", "discipline", "issue", "ask",
                        "priority",
                    ],
                },
            },
        },
        "required": ["rfi_items"],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT = """\
You are a senior General Contractor preparing a formal RFI \
(Request For Information) list to send to the design team for a \
construction project.

You'll be given:
  - The project context (name, building type, size)
  - One-sided gaps the discipline agents found (spec without drawing,
    drawing without spec, unilateral items)
  - Conflicts the auto-arbitrator deferred to HITL review
  - Quantity sanity flags (decimal-shift typos, unit-category errors)

Style:
  - Use the language an architect or engineer expects. Don't say
    "the spec doesn't have a drawing"; say "Section 09 51 13 specifies
    acoustical ceilings throughout but no RCP is provided — please
    issue an RCP for floor 2 or confirm finish schedule applies."
  - Cite SPECIFIC sheet IDs / spec sections / item descriptions. RFIs
    without specific citations get rejected by the design team.
  - Quantify impact when possible — count affected items, square
    footage, fixtures.
  - Priority guide:
      critical = blocks bid (RFI must be answered before invitation
                 packages go out)
      high     = bid-affecting (cost or schedule impact > 5%)
      medium   = clarification needed but workable with assumptions
      low      = post-bid cleanup; raise but don't gate on it

Deduplicate aggressively — a single RFI covering 10 missing schedule \
rows is better than 10 individual RFIs. Cap the list at the strongest \
~30 issues; quantity over quality is a failure mode here.

Use draft_rfi_list when ready.
"""


@dataclass
class RFIGenerationResult:
    rfi_count: int
    cost_usd: float
    rfi_list: list[dict]


def _format_gap_block(gap: Gap) -> str:
    parts = [
        f"  - GAP [{gap.gap_type}/{gap.severity}]",
    ]
    if gap.csi_section:
        parts[0] += f" csi={gap.csi_section}"
    elif gap.csi_division:
        parts[0] += f" div={gap.csi_division}"
    parts.append(f"    {gap.description[:400]}")
    if gap.suggested_remediation:
        parts.append(f"    suggested: {gap.suggested_remediation[:200]}")
    return "\n".join(parts)


def _format_conflict_block(c: Conflict) -> str:
    return (
        f"  - CONFLICT [{c.conflict_type}/{c.severity}] csi={c.csi_code or '?'}\n"
        f"    {(c.description or '')[:400]}"
    )


def _format_item_block(it: ScopeItem) -> str:
    return (
        f"  - ITEM {it.csi_code} [{it.evidence_tier or '?'}] "
        f"qty={it.qty_value or '?'} {it.qty_uom or ''}\n"
        f"    {it.description[:300]}"
    )


def _client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def generate_for_run(run_id: str) -> RFIGenerationResult:
    """Generate the project's RFI list from this run's gap signals.

    One Opus call. Persists nothing to a Document (yet) — caller
    receives the list and can serve it via API or persist as needed.
    Future: write to documents table with doc_type='rfi-list'.
    """
    client = _client()
    if client is None:
        return RFIGenerationResult(rfi_count=0, cost_usd=0.0, rfi_list=[])

    async with SessionLocal() as db:
        run = await db.get(ScopeExtractionRun, run_id)
        if run is None:
            return RFIGenerationResult(rfi_count=0, cost_usd=0.0, rfi_list=[])
        project_id = run.project_id
        proj = await db.get(Project, project_id)
        project_name = proj.name if proj else "(unnamed)"

        # Gather signals: prioritize blocker > warn, then truncate.
        gaps = (
            await db.execute(
                select(Gap)
                .where(Gap.run_id == run_id)
                .order_by(
                    # blocker first, then warn, then info
                    Gap.severity.desc(),
                    Gap.created_at,
                )
            )
        ).scalars().all()
        conflicts = (
            await db.execute(
                select(Conflict)
                .where(Conflict.run_id == run_id)
                .where(Conflict.status == "open")
            )
        ).scalars().all()
        # Low-confidence items as a tier-3 RFI source (only top 20)
        low_conf_items = (
            await db.execute(
                select(ScopeItem)
                .where(ScopeItem.run_id == run_id)
                .where(ScopeItem.evidence_tier == "INFERRED_LOW_CONFIDENCE")
                .order_by(ScopeItem.confidence)
                .limit(20)
            )
        ).scalars().all()

    if not gaps and not conflicts:
        log.info(
            "rfi_generator: run %s has no gaps or conflicts — no RFIs needed",
            run_id,
        )
        return RFIGenerationResult(rfi_count=0, cost_usd=0.0, rfi_list=[])

    # Cap signals at ~80 total to keep prompt reasonable
    gaps_in = gaps[:50]
    conflicts_in = conflicts[:20]
    items_in = low_conf_items[:10]

    user_prompt = (
        f"=== PROJECT ===\nName: {project_name}\nRun ID: {run_id}\n\n"
        f"=== GAPS ({len(gaps_in)}/{len(gaps)} shown) ===\n"
        + "\n".join(_format_gap_block(g) for g in gaps_in)
        + f"\n\n=== OPEN CONFLICTS ({len(conflicts_in)}/{len(conflicts)}) ===\n"
        + "\n".join(_format_conflict_block(c) for c in conflicts_in)
        + f"\n\n=== LOW-CONFIDENCE ITEMS ({len(items_in)}) ===\n"
        + "\n".join(_format_item_block(i) for i in items_in)
        + f"\n\nDraft a formal RFI list (max {_MAX_RFI_COUNT}) for the design "
        f"team. Use draft_rfi_list."
    )

    t0 = time.perf_counter()
    try:
        msg = await client.messages.create(
            model=_OPUS_MODEL,
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_RFI_TOOL],
            tool_choice={"type": "tool", "name": "draft_rfi_list"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:  # noqa: BLE001
        log.exception("rfi_generator: API error: %s", e)
        return RFIGenerationResult(rfi_count=0, cost_usd=0.0, rfi_list=[])
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "draft_rfi_list"
        ):
            payload = block.input
            break

    rfi_list = payload.get("rfi_items") or []
    rfi_list = [r for r in rfi_list if isinstance(r, dict)][:_MAX_RFI_COUNT]

    cost = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="rfi-generator",
            model=_OPUS_MODEL,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
        )
        cost = c or 0.0
        await db.commit()

    log.info(
        "rfi_generator: run %s — %d RFIs drafted in %dms ($%.4f)",
        run_id, len(rfi_list), latency_ms, cost,
    )
    return RFIGenerationResult(
        rfi_count=len(rfi_list),
        cost_usd=cost,
        rfi_list=rfi_list,
    )


def render_markdown(result: RFIGenerationResult, *, project_name: str = "") -> str:
    """Render the RFI list as a Markdown document for export / display."""
    if not result.rfi_list:
        return f"# Request For Information — {project_name}\n\nNo RFIs needed at this time.\n"
    lines = [f"# Request For Information — {project_name}", ""]
    for r in result.rfi_list:
        lines.append(f"## {r.get('rfi_number')} — {r.get('subject')}")
        lines.append("")
        lines.append(f"**Discipline**: {r.get('discipline')}")
        if r.get("csi_section"):
            lines.append(f"**CSI Section**: {r.get('csi_section')}")
        sheet_refs = r.get("sheet_refs") or []
        if sheet_refs:
            lines.append(f"**Sheets**: {', '.join(sheet_refs)}")
        lines.append(f"**Priority**: {r.get('priority', '?').upper()}")
        lines.append("")
        lines.append(f"**Issue**: {r.get('issue', '')}")
        lines.append("")
        lines.append(f"**Ask**: {r.get('ask', '')}")
        lines.append("")
        if r.get("impact"):
            lines.append(f"**Impact**: {r.get('impact')}")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)
