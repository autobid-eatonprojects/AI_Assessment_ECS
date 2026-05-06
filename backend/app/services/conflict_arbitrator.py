"""Stage 3 — conflict arbitrator.

For each open Conflict produced by ``conflict_detector``, send the cluster
members + their full cited chunks to Opus 4.7 and let it pick a winner.

Auto-arbitrates only when ALL members have agent confidence > 0.8. Lower-
confidence conflicts stay ``status='open'`` so an operator resolves them
in the HITL Conflicts queue (Stage 8/9). This keeps Opus cost bounded and
escalates ambiguous cases to humans.

For losers: ``verifier_status='rejected'`` is set on the loser ScopeItems
(per plan R7 — avoids inventing a new 'superseded' state). The conflict
itself records ``arbitrated_value`` (the winner's qty/unit/etc.) +
``arbitration_reasoning`` for audit.

Cost: roughly one Opus call per open conflict × ~$0.02. On a typical
project with 30-50 conflicts that's ~$0.50-1.00.
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
from ..models import (
    Chunk,
    Conflict,
    ConflictMember,
    ScopeCitation,
    ScopeItem,
)
from .audit import record_audit
from .llm_log import record_call, usage_from_anthropic

log = logging.getLogger(__name__)


_ARBITRATOR_MODEL = "claude-opus-4-7"
_ARBITRATOR_CONCURRENCY = 4
_AUTO_ARBITRATE_CONFIDENCE = 0.8


_ARBITRATE_TOOL = {
    "name": "arbitrate_conflict",
    "description": (
        "Pick the winning member of a scope-item conflict cluster. The "
        "winner is the member whose claim is best supported by the source "
        "chunks. Return the winner's scope_item_id and a one-sentence "
        "explanation. If genuinely cannot decide, set winner_id to null."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "winner_id": {
                "type": ["string", "null"],
                "description": "scope_item_id of the winning member, or null if undecided.",
            },
            "winning_qty": {
                "type": ["string", "null"],
                "description": "Quantity to use after resolution (winner's, possibly normalized).",
            },
            "winning_unit": {
                "type": ["string", "null"],
                "description": "Unit code to use after resolution.",
            },
            "reasoning": {
                "type": "string",
                "description": "1-2 sentence explanation of why this member wins.",
            },
        },
        "required": ["winner_id", "reasoning"],
    },
}


@dataclass
class _ArbCtx:
    conflict: Conflict
    members: list[ConflictMember]
    items_by_id: dict[str, ScopeItem]
    chunks_by_item: dict[str, list[Chunk]]


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


def _format_chunks(chunks: list[Chunk]) -> str:
    parts: list[str] = []
    for c in chunks:
        meta = c.extra or {}
        sheet = meta.get("sheet_number") or "—"
        page = c.page_number or "—"
        parts.append(
            f"[chunk_id={c.id} type={c.chunk_type} sheet={sheet} page={page}]\n"
            f"{c.text or ''}"
        )
    return "\n\n---\n\n".join(parts)


def _build_prompt(ctx: _ArbCtx) -> str:
    members_block: list[str] = []
    for idx, m in enumerate(ctx.members, 1):
        item = ctx.items_by_id.get(m.scope_item_id)
        if item is None:
            continue
        chunks = ctx.chunks_by_item.get(item.id, [])
        members_block.append(
            f"--- Member {idx} ---\n"
            f"scope_item_id: {item.id}\n"
            f"CSI code:      {item.csi_code} ({item.section_title or item.division_label})\n"
            f"Description:   {item.description}\n"
            f"Specification: {item.specification or '(none)'}\n"
            f"Quantity:      {item.quantity or '(none)'} {item.unit or ''}\n"
            f"Validator confidence: {item.confidence:.2f}\n"
            f"Citations:\n{_format_chunks(chunks)}"
        )
    body = "\n\n".join(members_block)

    return (
        f"You are a senior estimator arbitrating a scope-item conflict. "
        f"The members below are the same physical scope item according to "
        f"clustering, but they disagree on at least one of: quantity, unit, "
        f"or CSI code. Read the full citations and decide which member is "
        f"best supported by the evidence.\n\n"
        f"=== CONFLICT TYPE: {ctx.conflict.conflict_type} ===\n\n"
        f"=== MEMBERS ({len(ctx.members)}) ===\n{body}\n\n"
        f"=== TASK ===\n"
        f"Use arbitrate_conflict to return the winning scope_item_id with "
        f"reasoning. Set winner_id=null only if the evidence is genuinely "
        f"too thin to decide; in that case the conflict will be sent to "
        f"human review."
    )


async def _gather_open(run_id: str) -> tuple[list[_ArbCtx], str | None]:
    """Pull every open conflict + its members + cited chunks for one run."""
    async with SessionLocal() as db:
        conflicts = (
            await db.execute(
                select(Conflict)
                .where(Conflict.run_id == run_id)
                .where(Conflict.status == "open")
            )
        ).scalars().all()
        if not conflicts:
            return [], None

        conflict_ids = [c.id for c in conflicts]
        members = (
            await db.execute(
                select(ConflictMember).where(
                    ConflictMember.conflict_id.in_(conflict_ids)
                )
            )
        ).scalars().all()
        members_by_conflict: dict[str, list[ConflictMember]] = defaultdict(list)
        for m in members:
            members_by_conflict[m.conflict_id].append(m)

        item_ids = {m.scope_item_id for m in members}
        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.id.in_(item_ids))
            )
        ).scalars().all()
        items_by_id = {i.id: i for i in items}

        # All citations for those items + their chunks
        cit_rows = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_(item_ids)
                )
            )
        ).scalars().all()
        chunk_ids = {c.chunk_id for c in cit_rows}
        chunks = (
            await db.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
        ).scalars().all()
        chunks_by_id = {c.id: c for c in chunks}
        chunks_by_item: dict[str, list[Chunk]] = defaultdict(list)
        for c in cit_rows:
            ch = chunks_by_id.get(c.chunk_id)
            if ch is not None:
                chunks_by_item[c.scope_item_id].append(ch)

        contexts = [
            _ArbCtx(
                conflict=c,
                members=members_by_conflict.get(c.id, []),
                items_by_id=items_by_id,
                chunks_by_item=chunks_by_item,
            )
            for c in conflicts
            if members_by_conflict.get(c.id)
        ]
        # Project id from any conflict (all share same project under one run)
        project_id = conflicts[0].project_id
        return contexts, project_id


def _eligible_for_auto(ctx: _ArbCtx) -> bool:
    """Auto-arbitrate only when all members have confidence > 0.8."""
    items = [ctx.items_by_id.get(m.scope_item_id) for m in ctx.members]
    items = [i for i in items if i is not None]
    if not items:
        return False
    return all(i.confidence > _AUTO_ARBITRATE_CONFIDENCE for i in items)


async def _arbitrate_one(client, ctx: _ArbCtx, project_id: str) -> tuple[dict, float]:
    t0 = time.perf_counter()
    msg = await client.messages.create(
        model=_ARBITRATOR_MODEL,
        max_tokens=1024,
        tools=[_ARBITRATE_TOOL],
        tool_choice={"type": "tool", "name": "arbitrate_conflict"},
        messages=[{"role": "user", "content": _build_prompt(ctx)}],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "arbitrate_conflict"
        ):
            payload = block.input
            break

    cost: float = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="conflict-arbitrate",
            model=_ARBITRATOR_MODEL,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
        )
        cost = c or 0.0
        await db.commit()
    return payload, cost


async def arbitrate_run(run_id: str) -> tuple[int, int, float]:
    """Auto-arbitrate eligible open conflicts.

    Returns (arbitrated, deferred_to_hitl, total_cost_usd).
    """
    client = _get_client()
    if client is None:
        log.warning("conflict_arbitrator: no ANTHROPIC_API_KEY; deferring all")
        return 0, 0, 0.0

    contexts, project_id = await _gather_open(run_id)
    if not contexts or project_id is None:
        return 0, 0, 0.0

    eligible = [c for c in contexts if _eligible_for_auto(c)]
    deferred = len(contexts) - len(eligible)
    if not eligible:
        log.info(
            "conflict_arbitrator: %d conflicts, none eligible for auto-arb "
            "(all left for HITL)",
            len(contexts),
        )
        return 0, deferred, 0.0

    sem = asyncio.Semaphore(_ARBITRATOR_CONCURRENCY)

    async def with_sem(ctx: _ArbCtx):
        async with sem:
            try:
                payload, cost = await _arbitrate_one(client, ctx, project_id)
                return ctx, payload, cost
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "conflict_arbitrator: failed on conflict %s: %s",
                    ctx.conflict.id,
                    e,
                )
                return ctx, None, 0.0

    results = await asyncio.gather(*(with_sem(c) for c in eligible))

    arbitrated = 0
    total_cost = 0.0
    from datetime import datetime, timezone

    async with SessionLocal() as db:
        for ctx, payload, cost in results:
            total_cost += cost
            if payload is None:
                continue

            winner_id = payload.get("winner_id")
            reasoning = payload.get("reasoning") or ""
            winning_qty = payload.get("winning_qty")
            winning_unit = payload.get("winning_unit")

            # Update conflict row
            conflict = await db.get(Conflict, ctx.conflict.id)
            if conflict is None:
                continue

            if not winner_id or winner_id not in {
                m.scope_item_id for m in ctx.members
            }:
                # Model abstained or returned bogus id — leave for HITL
                continue

            conflict.status = "resolved"
            conflict.arbitrator = "opus-verifier"
            conflict.arbitration_reasoning = reasoning
            conflict.arbitrated_value = {
                "winner_scope_item_id": winner_id,
                "winning_qty": winning_qty,
                "winning_unit": winning_unit,
            }
            conflict.resolved_at = datetime.now(timezone.utc)

            # Mark members + reject losers
            for m in ctx.members:
                merged_member = await db.get(ConflictMember, m.id)
                if merged_member is None:
                    continue
                merged_member.is_winner = m.scope_item_id == winner_id
                if m.scope_item_id != winner_id:
                    loser = await db.get(ScopeItem, m.scope_item_id)
                    if loser is not None and loser.verifier_status != "rejected":
                        loser.verifier_status = "rejected"
                        loser.verifier_review = {
                            **(loser.verifier_review or {}),
                            "rejected_by": "conflict_arbitrator",
                            "conflict_id": conflict.id,
                            "winner_id": winner_id,
                            "reasoning": reasoning,
                        }

            await record_audit(
                db,
                project_id=project_id,
                run_id=run_id,
                action="resolve",
                entity_type="conflict",
                entity_id=conflict.id,
                actor="system:conflict_arbitrator",
                payload={
                    "conflict_type": conflict.conflict_type,
                    "winner_id": winner_id,
                    "members": [m.scope_item_id for m in ctx.members],
                    "winning_qty": winning_qty,
                    "winning_unit": winning_unit,
                },
                note=reasoning[:500],
            )
            arbitrated += 1
        await db.commit()

    log.info(
        "conflict_arbitrator: run %s — %d auto-arbitrated, %d deferred to HITL, $%.3f",
        run_id,
        arbitrated,
        deferred,
        total_cost,
    )
    return arbitrated, deferred, total_cost
