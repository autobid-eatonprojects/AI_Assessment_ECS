"""Phase 8b — Coverage matrix scorer.

For every (scope_item, bid) pair, decide whether this bid covers that
scope item:

  - covered:        a line in the bid clearly satisfies the scope item
  - partial:        the bid covers part but explicitly excludes related work
  - excluded:       the bid covers neither the item nor an explicit exclusion
                    that mentions it (i.e. the vendor walked the scope and
                    silently left it out)
  - not_applicable: scope item is in a CSI division this bid doesn't touch
                    (e.g. checking a roofing scope item against a concrete
                    bid). These are filtered before calling the model.

Naïve approach (one Haiku call per (scope, bid) pair) is up to
~600 items × 10 bids = 6,000 calls — too expensive even at Haiku rates.
Optimizations:

1. **CSI division pre-filter**: only score pairs where the bid's
   primary_csi_divisions overlaps the scope item's csi_division. Cuts
   ~80% of pairs.
2. **Haiku-first**: each surviving pair gets one Haiku call (judge
   coverage with the bid's line items + exclusions as context). No
   Sonnet escalation in the v1 — we surface low-confidence calls in
   the UI for operator review instead.
3. **Per-bid context cache**: each bid's line items / exclusions /
   inclusions are formatted into a single context block once per bid,
   reused across all scope items in the inner loop.

Cost estimate for a small commercial project (~600 scope items × ~5 priced
bids = 3000 pairs): after CSI pre-filter ~600 pairs survive; 600 Haiku
calls × ~$0.0005 ≈ $0.30. Larger projects scale roughly linearly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings
from ..models import (
    BidCoverage,
    BidExclusion,
    BidInclusion,
    BidLineItem,
    BidSummary,
    ScopeItem,
)
from .llm_log import Usage, record_call, usage_from_anthropic

log = logging.getLogger(__name__)


_COVERAGE_CONCURRENCY = 12


_COVERAGE_TOOL = {
    "name": "judge_coverage",
    "description": (
        "Decide whether a vendor bid covers a single scope item. The bid "
        "context lists every line item the vendor priced, plus their "
        "stated inclusions and exclusions. The scope item is one row from "
        "the GC's scope of work. Use professional estimating judgement."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["covered", "partial", "excluded", "not_covered"],
                "description": (
                    "covered = a bid line clearly satisfies the scope item; "
                    "partial = the bid touches the item but excludes related "
                    "work; excluded = the bid explicitly excludes this item "
                    "(or its category); not_covered = the bid never mentions "
                    "this item or anything that satisfies it."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0.0 unsure, 1.0 certain.",
            },
            "matched_line_index": {
                "type": "integer",
                "description": (
                    "0-based index into the supplied line_items list of the "
                    "line that covers this scope item (status=covered or "
                    "partial). Null if no line covers it."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence justifying the decision.",
            },
        },
        "required": ["status", "confidence", "reasoning"],
    },
}


@dataclass
class _BidContext:
    """Per-bid context built once + reused for every scope item we score."""

    bid_document_id: str
    summary: BidSummary
    line_items: list[BidLineItem]
    inclusions: list[BidInclusion]
    exclusions: list[BidExclusion]
    primary_divisions: set[str]
    text_block: str  # rendered for the prompt


def _format_bid_context(
    summary: BidSummary,
    lines: list[BidLineItem],
    incs: list[BidInclusion],
    excs: list[BidExclusion],
) -> str:
    parts: list[str] = []
    parts.append(f"Vendor: {summary.vendor_name or '(unknown)'}")
    if summary.bid_total_usd:
        parts.append(f"Bid total: ${summary.bid_total_usd:,.2f}")
    if summary.primary_csi_divisions:
        parts.append(
            "Primary CSI divisions: " + ", ".join(summary.primary_csi_divisions)
        )

    if lines:
        parts.append("\nLine items (price = unit×qty or stated total):")
        for i, ln in enumerate(lines):
            qty = f" {ln.quantity} {ln.unit}" if ln.quantity else ""
            price = (
                f" — ${ln.total_price_usd:,.2f}"
                if ln.total_price_usd
                else f" — ${ln.unit_price_usd:,.2f}/{ln.unit}" if ln.unit_price_usd else ""
            )
            csi = f" [csi:{ln.csi_section_guess}]" if ln.csi_section_guess else ""
            parts.append(f"  [{i}] {ln.description}{qty}{price}{csi}")
    else:
        parts.append("\nLine items: (none parsed)")

    if incs:
        parts.append("\nExplicit inclusions:")
        for inc in incs:
            parts.append(f"  - {inc.text}")
    if excs:
        parts.append("\nExplicit exclusions:")
        for exc in excs:
            parts.append(f"  - {exc.text}")
    return "\n".join(parts)


def _get_client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _build_bid_contexts(
    project_id: str, run_id: str
) -> list[_BidContext]:
    """One _BidContext per priced bid in the run."""
    from ..database import SessionLocal

    contexts: list[_BidContext] = []
    async with SessionLocal() as db:
        summaries = (
            await db.execute(
                select(BidSummary)
                .where(BidSummary.project_id == project_id)
                .where(BidSummary.run_id == run_id)
            )
        ).scalars().all()

        for s in summaries:
            lines = list(
                (
                    await db.execute(
                        select(BidLineItem)
                        .where(BidLineItem.bid_document_id == s.bid_document_id)
                        .where(BidLineItem.run_id == run_id)
                    )
                ).scalars().all()
            )
            incs = list(
                (
                    await db.execute(
                        select(BidInclusion)
                        .where(BidInclusion.bid_document_id == s.bid_document_id)
                        .where(BidInclusion.run_id == run_id)
                    )
                ).scalars().all()
            )
            excs = list(
                (
                    await db.execute(
                        select(BidExclusion)
                        .where(BidExclusion.bid_document_id == s.bid_document_id)
                        .where(BidExclusion.run_id == run_id)
                    )
                ).scalars().all()
            )
            divs = set(s.primary_csi_divisions or [])
            text_block = _format_bid_context(s, lines, incs, excs)
            contexts.append(
                _BidContext(
                    bid_document_id=s.bid_document_id,
                    summary=s,
                    line_items=lines,
                    inclusions=incs,
                    exclusions=excs,
                    primary_divisions=divs,
                    text_block=text_block,
                )
            )
    return contexts


def _is_relevant(scope: ScopeItem, ctx: _BidContext) -> bool:
    """Pre-filter: score only when the bid claims to touch this division.

    If the bid didn't declare any primary divisions (fallback), score
    everything — better to overscore than miss coverage.
    """
    if not ctx.primary_divisions:
        return True
    return scope.csi_division in ctx.primary_divisions


async def _score_one_pair(
    client,
    scope: ScopeItem,
    ctx: _BidContext,
    project_id: str,
    sem: asyncio.Semaphore,
) -> tuple[ScopeItem, _BidContext, dict, Usage, int]:
    """Single Haiku call for one (scope, bid) pair."""
    prompt = (
        f"=== BID CONTEXT ===\n{ctx.text_block}\n\n"
        f"=== SCOPE ITEM ===\n"
        f"CSI: {scope.csi_code} ({scope.section_title or scope.division_label})\n"
        f"Description: {scope.description}\n"
        f"Quantity: {scope.quantity or '(not stated)'} {scope.unit or ''}\n"
        f"Spec: {scope.specification or '(none)'}\n\n"
        f"=== TASK ===\n"
        f"Decide whether this vendor bid covers the scope item. If a "
        f"specific line clearly covers it, return status=covered and the "
        f"line index. If the bid explicitly excludes it (or its category), "
        f"return status=excluded. If the bid touches the area but omits "
        f"key parts, status=partial. Otherwise status=not_covered. Use "
        f"the judge_coverage tool now."
    )
    async with sem:
        t0 = time.perf_counter()
        msg = await client.messages.create(
            model=settings.classifier_model,  # Haiku 4.5
            max_tokens=512,
            tools=[_COVERAGE_TOOL],
            tool_choice={"type": "tool", "name": "judge_coverage"},
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

    verdict: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "judge_coverage"
        ):
            verdict = block.input
            break
    return scope, ctx, verdict, usage_from_anthropic(msg), latency_ms


async def score_coverage(
    project_id: str,
    run_id: str,
    progress_cb=None,
) -> tuple[int, float]:
    """Score every applicable (scope_item, bid) pair for the latest scope run.

    Returns (pair_count, total_cost_usd).

    `progress_cb` is an awaitable hook for the orchestrator to bump
    coverage_pairs_completed in the run row.
    """
    client = _get_client()
    if client is None:
        log.warning("bid_coverage: no ANTHROPIC_API_KEY")
        return 0, 0.0

    contexts = await _build_bid_contexts(project_id, run_id)
    if not contexts:
        log.info("bid_coverage: no bid summaries in run %s — skipping scoring", run_id)
        return 0, 0.0

    # Get every scope item from the latest scope run. Coverage analysis is
    # always anchored to the most recent scope (Phase 4) extraction.
    from ..database import SessionLocal
    from ..models import ScopeExtractionRun

    async with SessionLocal() as db:
        latest_scope = (
            await db.execute(
                select(ScopeExtractionRun)
                .where(ScopeExtractionRun.project_id == project_id)
                .where(ScopeExtractionRun.status == "complete")
                .order_by(ScopeExtractionRun.completed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_scope is None:
            log.warning("bid_coverage: no completed scope run for project %s", project_id)
            return 0, 0.0
        scope_items = list(
            (
                await db.execute(
                    select(ScopeItem).where(ScopeItem.run_id == latest_scope.id)
                )
            ).scalars().all()
        )

    # Build scoring tasks (post pre-filter)
    pairs: list[tuple[ScopeItem, _BidContext]] = []
    for scope in scope_items:
        for ctx in contexts:
            if _is_relevant(scope, ctx):
                pairs.append((scope, ctx))

    log.info(
        "bid_coverage: %d (scope, bid) pairs to score (after CSI filter); "
        "%d scope items × %d bids = %d max",
        len(pairs),
        len(scope_items),
        len(contexts),
        len(scope_items) * len(contexts),
    )

    sem = asyncio.Semaphore(_COVERAGE_CONCURRENCY)
    completed = 0

    async def run_one(scope, ctx):
        nonlocal completed
        try:
            result = await _score_one_pair(client, scope, ctx, project_id, sem)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "bid_coverage: pair (scope %s, bid %s) failed: %s",
                scope.id,
                ctx.bid_document_id,
                e,
            )
            return None
        completed += 1
        if progress_cb is not None:
            await progress_cb(completed)
        return result

    results = await asyncio.gather(*(run_one(s, c) for s, c in pairs))

    # Persist + accounting
    total_cost = 0.0
    async with SessionLocal() as db:
        for r in results:
            if r is None:
                continue
            scope, ctx, verdict, usage, latency_ms = r
            cost, _ = await record_call(
                db,
                purpose="bid-coverage",
                model=settings.classifier_model,
                usage=usage,
                latency_ms=latency_ms,
                project_id=project_id,
                document_id=ctx.bid_document_id,
            )
            total_cost += cost or 0.0

            status = (verdict.get("status") or "not_covered").lower()
            confidence = float(verdict.get("confidence", 0.0) or 0.0)
            reasoning = verdict.get("reasoning")
            matched_idx_raw = verdict.get("matched_line_index")
            matched_line_id: str | None = None
            try:
                matched_idx = (
                    int(matched_idx_raw) if matched_idx_raw is not None else None
                )
            except (TypeError, ValueError):
                matched_idx = None
            if matched_idx is not None and 0 <= matched_idx < len(ctx.line_items):
                matched_line_id = ctx.line_items[matched_idx].id

            db.add(
                BidCoverage(
                    project_id=project_id,
                    run_id=run_id,
                    scope_item_id=scope.id,
                    bid_document_id=ctx.bid_document_id,
                    status=status,
                    confidence=confidence,
                    reasoning=reasoning,
                    matched_line_item_id=matched_line_id,
                    judge_model=settings.classifier_model,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                )
            )
        await db.commit()

    log.info(
        "bid_coverage: scored %d pairs, $%.4f total",
        len([r for r in results if r is not None]),
        total_cost,
    )
    return len(pairs), total_cost
