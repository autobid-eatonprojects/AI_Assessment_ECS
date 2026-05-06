"""P6 — per-package bid-invitation narrative writer.

After the trade_bundler assigns every ScopeItem to a TradePackage, this
service drafts a short Markdown narrative per package — the kind of
cover letter a GC includes when emailing a bid invitation to subs:

    Subject: <Project> — Concrete & Masonry bid invitation
    Body:    one-paragraph context, scope summary, key qty highlights,
             callouts for any spec mandates without drawing evidence,
             RFI invitation language.

One Haiku 4.5 call per TradePackage (cheap — typically <$0.01 each).
Persisted to TradePackage.narrative_md so the API can serve it as the
package detail body. Idempotent: re-running a scope_extraction wipes
+ rewrites every package's narrative.

Cost on a typical mid-size project: ~10-15 packages × ~$0.005 ≈ $0.10.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select, update

from ..config import settings
from ..database import SessionLocal
from ..models import (
    Project,
    ProjectProfile,
    ScopeItem,
    TradePackage,
)
from .llm_log import record_call, usage_from_anthropic

log = logging.getLogger(__name__)


_MAX_ITEMS_IN_PROMPT = 60  # Cap per package to keep prompts cheap
_CONCURRENCY = 5  # Haiku is fast; 5 parallel keeps total wall-clock low


_DRAFT_TOOL = {
    "name": "draft_bid_invitation",
    "description": (
        "Draft a concise Markdown bid-invitation narrative for one trade "
        "package. The reader is a subcontractor receiving the invitation; "
        "they need to understand the scope at a glance and decide whether "
        "to bid. Cover: project context (1 line), package scope summary, "
        "any high-quantity highlights, any spec mandates lacking drawing "
        "evidence (so the bidder knows to clarify), and an RFI invitation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject_line": {
                "type": "string",
                "description": (
                    "Short email subject — e.g. 'Elks Community Center — "
                    "Concrete & Masonry bid invitation'. Under 80 chars."
                ),
            },
            "body_md": {
                "type": "string",
                "description": (
                    "Markdown body of the invitation. 4-8 short paragraphs. "
                    "Use `## Scope summary` / `## Key quantities` / "
                    "`## Open questions for bidder` headers. Ground every "
                    "claim in the items provided — don't invent quantities "
                    "or specs that aren't in the input."
                ),
            },
        },
        "required": ["subject_line", "body_md"],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT = """\
You are a senior construction estimator helping a General Contractor \
write bid-invitation cover letters for trade packages. Each package is \
a bundle of scope items the GC wants subcontractors to bid on.

Style:
  - Professional but not stiff. Subcontractors read these fast.
  - Be SPECIFIC — name materials, qty signals, location callouts the
    bidder cares about. Not "various concrete work" — say "1,200 SF
    of slab-on-grade with 3-inch insulation per S2.1".
  - Cite csi_code in parens when relevant ("(03 30 00)").
  - If items have qty_confidence='conflicting' or 'unverified', flag
    that for the bidder to clarify in their proposal.
  - If the package contains items flagged with one-sided evidence
    (drawing without spec, etc.), surface that under "Open questions".

Don't invent quantities, specs, or sheet references. Only use what's \
in the package_items input.

Use draft_bid_invitation when ready.
"""


def _client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _format_item(item: ScopeItem) -> str:
    qty = item.quantity or "—"
    unit = item.unit or ""
    qty_str = f"{qty} {unit}".strip()
    section = item.section_title or item.division_label or "?"
    flags: list[str] = []
    if item.qty_confidence and item.qty_confidence not in ("high",):
        flags.append(f"qty:{item.qty_confidence}")
    if item.evidence_tier == "INFERRED_LOW_CONFIDENCE":
        flags.append("evidence:low")
    if item.bilateral_evidence is False:
        flags.append("one-sided")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    return (
        f"  - {item.csi_code} ({section}): {item.description}"
        + (f" — qty {qty_str}" if qty_str != "—" else "")
        + (f" @ {item.location}" if item.location else "")
        + flag_str
    )


async def _draft_one_package(
    client,
    *,
    project_id: str,
    project_name: str,
    profile_summary: str,
    pkg: TradePackage,
    items: list[ScopeItem],
) -> tuple[str | None, str | None, float]:
    """Draft narrative for one package. Returns (subject, body_md, cost_usd)."""
    if not items:
        return None, None, 0.0

    # Items already sorted by csi_code in caller. Cap to keep prompt small.
    item_block = "\n".join(_format_item(i) for i in items[:_MAX_ITEMS_IN_PROMPT])
    truncation_note = (
        f"\n  …({len(items) - _MAX_ITEMS_IN_PROMPT} more items omitted)"
        if len(items) > _MAX_ITEMS_IN_PROMPT
        else ""
    )

    prompt = (
        f"=== PROJECT ===\n"
        f"Name: {project_name}\n"
        f"Context: {profile_summary or '(no profile available)'}\n\n"
        f"=== TRADE PACKAGE ===\n"
        f"Label: {pkg.package_label}\n"
        f"CSI Divisions: {', '.join(pkg.csi_divisions or [])}\n"
        f"Item count: {pkg.item_count}\n"
        f"Bilateral coverage: {pkg.bilateral_count}/{pkg.item_count}\n"
        f"Avg confidence: {pkg.avg_confidence or 0.0:.2f}\n\n"
        f"=== PACKAGE ITEMS ===\n{item_block}{truncation_note}\n\n"
        f"Draft a bid-invitation cover letter (subject + Markdown body) "
        f"that gives a bidder enough to decide whether to bid + start "
        f"a takeoff. Use draft_bid_invitation."
    )

    t0 = time.perf_counter()
    try:
        msg = await client.messages.create(
            model=settings.classifier_model,  # Haiku 4.5
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_DRAFT_TOOL],
            tool_choice={"type": "tool", "name": "draft_bid_invitation"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "package_narrative: API error on package %s: %s", pkg.package_key, e
        )
        return None, None, 0.0
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "draft_bid_invitation"
        ):
            payload = block.input
            break

    cost = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="package-narrative",
            model=settings.classifier_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
        )
        cost = c or 0.0
        await db.commit()

    subject = (payload.get("subject_line") or "").strip() or None
    body = (payload.get("body_md") or "").strip() or None
    return subject, body, cost


async def write_narratives_for_run(run_id: str) -> tuple[int, float]:
    """Generate narrative_md for every TradePackage in this run.

    Returns (packages_written, total_cost_usd). Skips packages with 0
    items (the trade_bundler may produce them as placeholders).
    """
    client = _client()
    if client is None:
        log.info("package_narrative: no Anthropic key — skipping")
        return 0, 0.0

    async with SessionLocal() as db:
        packages = (
            await db.execute(
                select(TradePackage).where(TradePackage.run_id == run_id)
            )
        ).scalars().all()
        if not packages:
            return 0, 0.0
        project_id = packages[0].project_id
        proj = await db.get(Project, project_id)
        project_name = proj.name if proj else "(unnamed)"
        profile = (
            await db.execute(
                select(ProjectProfile).where(ProjectProfile.project_id == project_id)
            )
        ).scalar_one_or_none()
        profile_summary = ""
        if profile is not None:
            profile_summary = (
                f"{profile.building_type or 'building'}, "
                f"{profile.size_sf or '?'} SF, "
                f"{profile.occupancy or '?'} occupancy, "
                f"{profile.stories or '?'} stories"
            )

        # Fetch items per package once. We reuse the cached items so each
        # narrative write isn't a fresh DB roundtrip per package.
        items_by_pkg: dict[str, list[ScopeItem]] = {}
        for pkg in packages:
            rows = (
                await db.execute(
                    select(ScopeItem)
                    .where(ScopeItem.run_id == run_id)
                    .where(ScopeItem.package_id == pkg.id)
                    .order_by(ScopeItem.csi_code)
                )
            ).scalars().all()
            items_by_pkg[pkg.id] = list(rows)

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def with_sem(pkg: TradePackage):
        async with sem:
            return await _draft_one_package(
                client,
                project_id=project_id,
                project_name=project_name,
                profile_summary=profile_summary,
                pkg=pkg,
                items=items_by_pkg.get(pkg.id, []),
            )

    results = await asyncio.gather(*(with_sem(p) for p in packages))

    # Persist narratives via UPDATE (one per row to keep it simple)
    written = 0
    total_cost = 0.0
    async with SessionLocal() as db:
        for pkg, (subject, body, cost) in zip(packages, results):
            if body is None:
                continue
            # Embed the subject line at the top of the body so the API
            # consumer can still extract it without a separate column.
            full_md = (
                f"<!-- subject: {subject} -->\n\n# {pkg.package_label}\n\n{body}"
                if subject
                else f"# {pkg.package_label}\n\n{body}"
            )
            await db.execute(
                update(TradePackage)
                .where(TradePackage.id == pkg.id)
                .values(narrative_md=full_md)
            )
            written += 1
            total_cost += cost
        await db.commit()

    log.info(
        "package_narrative: wrote %d/%d narratives for run %s ($%.4f)",
        written, len(packages), run_id, total_cost,
    )
    return written, total_cost
