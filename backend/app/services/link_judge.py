"""Stage 3 — link judge: per-citation entailment check.

For every persisted ScopeItem in a run, ask Haiku 4.5 (per the plan's
P4 Haiku-judge step): "given this item's claim and these cited chunks,
is the claim ENTAILED, NEUTRAL, or CONTRADICTED?" One call per item with
all of that item's citations rendered in full.

Outputs:
    scope_citations.is_link_judge_pass : bool
        True iff the judge's verdict for the citation's chunk was ENTAILED.
        False if NEUTRAL or CONTRADICTED.
    scope_citations.link_judge_score   : float in [0, 1]
        The judge's stated confidence in the verdict.

Why per-item not per-citation calls:
    Citations are correlated (same item, related chunks). Asking the model
    once with all citations lets it weigh them against each other and is
    cheaper. The tool-use schema returns a per-citation array.

Cost:
    ~1 Haiku call per scope item × ~$0.003. On a small commercial project
    (~600 items) that's ~$2; scales linearly with item count.

This is the third validation pass alongside the existing 3-vote validator
(during extraction) and Opus verifier (post-persistence on flagged items).
The link judge is the *first* one that operates on the actual cited chunks
(not retrieved candidates) — so it catches "good item, wrong citation" cases
the other two miss.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Chunk, ScopeCitation, ScopeItem
from .llm_log import record_call, usage_from_anthropic

log = logging.getLogger(__name__)


_LINK_JUDGE_CONCURRENCY = 10

# Tool-use schema. Returns one verdict per citation, indexed by citation_id.
_LINK_JUDGE_TOOL = {
    "name": "judge_citations",
    "description": (
        "Decide whether each cited chunk genuinely supports the claimed "
        "scope item. ENTAILED = the chunk explicitly mentions or directly "
        "implies the claim. NEUTRAL = the chunk is related but doesn't "
        "really support the specific claim. CONTRADICTED = the chunk says "
        "something incompatible with the claim. Be strict — only ENTAILED "
        "if a senior estimator would point at this chunk as evidence for "
        "the item."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "citation_id": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["ENTAILED", "NEUTRAL", "CONTRADICTED"],
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "One short sentence per citation.",
                        },
                    },
                    "required": ["citation_id", "verdict", "confidence", "reasoning"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


@dataclass
class _ItemCtx:
    """Bundled inputs for a single judge call."""

    item: ScopeItem
    citations: list[ScopeCitation]
    chunks_by_id: dict[str, Chunk]


_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _format_citations(ctx: _ItemCtx) -> str:
    """Render each citation in full for the model to entailment-check."""
    parts: list[str] = []
    for c in ctx.citations:
        chunk = ctx.chunks_by_id.get(c.chunk_id)
        text = (chunk.text if chunk else (c.excerpt or "(chunk text unavailable)")) or ""
        meta = (chunk.extra if chunk else None) or {}
        sheet = meta.get("sheet_number") or c.sheet_number or "—"
        page = c.page_number or (chunk.page_number if chunk else None) or "—"
        parts.append(
            f"[citation_id={c.id} sheet={sheet} page={page} "
            f"evidence_type={c.evidence_type or '—'}]\n{text}"
        )
    return "\n\n---\n\n".join(parts)


def _build_prompt(ctx: _ItemCtx) -> str:
    item = ctx.item
    citations_block = _format_citations(ctx)
    return (
        f"You are a senior estimator entailment-checking the citations on a "
        f"scope-of-work item. For each citation, decide whether its chunk "
        f"genuinely supports the item.\n\n"
        f"=== SCOPE ITEM ===\n"
        f"CSI code:      {item.csi_code} ({item.section_title or item.division_label})\n"
        f"Description:   {item.description}\n"
        f"Specification: {item.specification or '(none)'}\n"
        f"Quantity:      {item.quantity or '(none)'} {item.unit or ''}\n"
        f"Location:      {item.location or '(none)'}\n\n"
        f"=== CITATIONS ({len(ctx.citations)}) ===\n"
        f"{citations_block}\n\n"
        f"=== TASK ===\n"
        f"Use judge_citations to return one verdict per citation. Use the "
        f"exact citation_id values shown above. ENTAILED only when the "
        f"chunk clearly mentions or directly implies the claim — be strict."
    )


async def _judge_one(client, ctx: _ItemCtx, project_id: str) -> tuple[list[dict], float]:
    t0 = time.perf_counter()
    # Cache the tool schema across the per-item judge calls in this run.
    # ~600 items per project means cache hit on calls 2..N — break-even
    # at 2 calls, so essentially every project benefits.
    cached_tool = {**_LINK_JUDGE_TOOL, "cache_control": {"type": "ephemeral"}}
    msg = await client.messages.create(
        model=settings.classifier_model,  # Haiku 4.5
        max_tokens=2048,
        tools=[cached_tool],
        tool_choice={"type": "tool", "name": "judge_citations"},
        messages=[{"role": "user", "content": _build_prompt(ctx)}],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    verdicts: list[dict] = []
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "judge_citations"
        ):
            verdicts = block.input.get("verdicts", []) or []
            break

    cost: float = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="link-judge",
            model=settings.classifier_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
        )
        cost = c or 0.0
        await db.commit()
    return verdicts, cost


async def _gather_run(run_id: str) -> tuple[list[_ItemCtx], str | None]:
    """Pull every item, its citations, and its cited chunks for one run."""
    async with SessionLocal() as db:
        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        if not items:
            return [], None

        item_ids = [i.id for i in items]
        cit_rows = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_(item_ids)
                )
            )
        ).scalars().all()
        cits_by_item: dict[str, list[ScopeCitation]] = defaultdict(list)
        chunk_ids: set[str] = set()
        for c in cit_rows:
            cits_by_item[c.scope_item_id].append(c)
            chunk_ids.add(c.chunk_id)

        chunk_rows = (
            await db.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
        ).scalars().all()
        chunks_by_id: dict[str, Chunk] = {c.id: c for c in chunk_rows}

        contexts = [
            _ItemCtx(
                item=item,
                citations=cits_by_item.get(item.id, []),
                chunks_by_id=chunks_by_id,
            )
            for item in items
            if cits_by_item.get(item.id)
        ]
        project_id = items[0].project_id
        return contexts, project_id


async def judge_run(run_id: str) -> tuple[int, int, float]:
    """Run the link judge over every item in a run.

    Returns (citations_judged, citations_passed, total_cost_usd).
    """
    client = _get_client()
    if client is None:
        log.warning("link_judge: no ANTHROPIC_API_KEY; skipping run %s", run_id)
        return 0, 0, 0.0

    contexts, project_id = await _gather_run(run_id)
    if not contexts or project_id is None:
        log.info("link_judge: nothing to judge for run %s", run_id)
        return 0, 0, 0.0

    sem = asyncio.Semaphore(_LINK_JUDGE_CONCURRENCY)

    async def judge_with_sem(ctx: _ItemCtx):
        async with sem:
            try:
                return await _judge_one(client, ctx, project_id), ctx
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "link_judge: failed on item %s: %s", ctx.item.id, e
                )
                return None, ctx

    results = await asyncio.gather(*(judge_with_sem(c) for c in contexts))

    # Apply verdicts to citations + record cost. One commit at the end.
    total_cost = 0.0
    judged_count = 0
    pass_count = 0
    async with SessionLocal() as db:
        for outcome, ctx in results:
            if outcome is None:
                continue
            verdicts, cost = outcome
            total_cost += cost
            verdict_by_cid = {v.get("citation_id"): v for v in verdicts}
            for cit in ctx.citations:
                v = verdict_by_cid.get(cit.id)
                if v is None:
                    continue
                verdict_str = (v.get("verdict") or "").upper()
                conf = float(v.get("confidence") or 0.0)
                passed = verdict_str == "ENTAILED"
                # Re-fetch via session to issue UPDATE
                merged = await db.get(ScopeCitation, cit.id)
                if merged is None:
                    continue
                merged.is_link_judge_pass = passed
                merged.link_judge_score = conf
                judged_count += 1
                if passed:
                    pass_count += 1
        await db.commit()

    log.info(
        "link_judge: run %s — %d/%d citations passed (%.0f%%), $%.3f",
        run_id,
        pass_count,
        judged_count,
        (pass_count / judged_count * 100) if judged_count else 0,
        total_cost,
    )
    return judged_count, pass_count, total_cost
