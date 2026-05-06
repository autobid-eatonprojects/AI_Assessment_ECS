"""Phase 7 — Opus 4.7 reflection pass on flagged scope items (Stage F).

The 3-vote Haiku validator (Stage B) gives every item a confidence score.
This stage takes a more expensive second look at items the validator
flagged as low-quality, plus items the quantity resolver flagged as
conflicting:

  - confidence < 0.6  (red, validator wasn't confident)
  - qty_confidence == "conflicting"  (sources disagreed numerically)

For each flagged item, Opus 4.7 receives:
  - The full citation chunks (not truncated like the validator)
  - The current ScopeItem fields
  - The resolved quantity + provenance
and returns one of three verdicts:

  - keep      : evidence does support the item as written
  - revised   : evidence supports a CORRECTED version (model rewrites
                description / quantity / unit accordingly)
  - rejected  : evidence does NOT support the item; mark for soft-delete

We deliberately don't run Opus on green items — most of the value is in
catching the validator's misses, and Opus on every item would be
prohibitively expensive (~$15-25 on a 600-item run). On a typical project
the flagged subset is ~5-10% of items, so the verifier costs ~$1-2.

Audit: every Opus output (verdict + reasoning + revised values) is
persisted to ScopeItem.verifier_review JSON; the verdict itself goes to
verifier_status. UI can filter by status.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import or_, select

from ..config import settings
from ..models import Chunk, ScopeCitation, ScopeItem
from .llm_log import Usage, record_call, usage_from_anthropic

log = logging.getLogger(__name__)


_VERIFIER_MODEL = "claude-opus-4-7"
_VERIFIER_CONCURRENCY = 4
_FLAGGED_CONFIDENCE_THRESHOLD = 0.6


_VERIFY_TOOL = {
    "name": "judge_and_revise",
    "description": (
        "Take a careful second look at a scope item flagged by the "
        "primary validator. Read the full source chunks (untruncated). "
        "Decide whether the item is genuinely supported AS-WRITTEN, "
        "supported only after a revision, or unsupported (hallucination)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["keep", "revised", "rejected"],
                "description": (
                    "keep = item is correct as written; "
                    "revised = source supports a corrected version (return "
                    "the revised values); "
                    "rejected = source does not support this item at all."
                ),
            },
            "revised_description": {
                "type": "string",
                "description": (
                    "Replacement description (only when verdict=revised). "
                    "Be concise and specific enough to bid against."
                ),
            },
            "revised_quantity": {
                "type": "string",
                "description": "Replacement quantity (verdict=revised). Null to leave unchanged.",
            },
            "revised_unit": {
                "type": "string",
                "description": "Replacement unit (verdict=revised). Null to leave unchanged.",
            },
            "consistency_check": {
                "type": "object",
                "description": (
                    "Cross-source consistency note. Set fields you can "
                    "support from the chunks; leave others null."
                ),
                "properties": {
                    "schedule_says": {"type": "string"},
                    "note_says": {"type": "string"},
                    "spec_says": {"type": "string"},
                    "agree": {"type": "boolean"},
                },
            },
            "reasoning": {
                "type": "string",
                "description": "1-2 sentence explanation of the verdict.",
            },
        },
        "required": ["verdict", "reasoning"],
    },
}


@dataclass
class _FlaggedItem:
    item: ScopeItem
    chunks: list[Chunk]
    citations: list[ScopeCitation]
    flag_reason: str


def _get_client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _format_chunks(chunks: list[Chunk]) -> str:
    """Render chunks for the prompt with FULL text (no truncation)."""
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


def _build_prompt(flagged: _FlaggedItem) -> str:
    item = flagged.item
    chunks = _format_chunks(flagged.chunks)
    qty_str = f"{item.quantity or 'null'} {item.unit or ''}".strip()
    qty_band = item.qty_confidence or "(not resolved)"

    return (
        f"=== FLAGGED SCOPE ITEM ===\n"
        f"Reason flagged: {flagged.flag_reason}\n"
        f"CSI code:      {item.csi_code} ({item.section_title or item.division_label})\n"
        f"Description:   {item.description}\n"
        f"Specification: {item.specification or '(none)'}\n"
        f"Quantity:      {qty_str}  [confidence: {qty_band}]\n"
        f"Validator confidence: {item.confidence:.2f}\n"
        f"Extraction method: {item.extraction_method or '(unknown)'}\n\n"
        f"=== FULL EVIDENCE CHUNKS ({len(flagged.chunks)}) ===\n"
        f"{chunks}\n\n"
        f"=== TASK ===\n"
        f"Apply senior estimator judgement. Decide whether the item as "
        f"stated is genuinely supported by the evidence above. If a "
        f"slightly revised version IS supported (e.g. wrong quantity, "
        f"unclear description), return verdict=revised with corrected "
        f"values. Only return verdict=rejected if the chunks don't "
        f"actually support this scope item at all.\n\n"
        f"Use the judge_and_revise tool now."
    )


async def _verify_one(
    client,
    project_id: str,
    flagged: _FlaggedItem,
    sem: asyncio.Semaphore,
) -> tuple[ScopeItem, dict | None, Usage, int]:
    async with sem:
        t0 = time.perf_counter()
        # Cache the tool schema across flagged items in this run. Opus
        # has a 1024-tok cache minimum which the schema satisfies.
        cached_tool = {**_VERIFY_TOOL, "cache_control": {"type": "ephemeral"}}
        msg = await client.messages.create(
            model=_VERIFIER_MODEL,
            max_tokens=2048,
            tools=[cached_tool],
            tool_choice={"type": "tool", "name": "judge_and_revise"},
            messages=[{"role": "user", "content": _build_prompt(flagged)}],
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict | None = None
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "judge_and_revise"
        ):
            payload = block.input
            break
    return flagged.item, payload, usage_from_anthropic(msg), latency_ms


async def _gather_flagged(run_id: str) -> list[_FlaggedItem]:
    """Pull every red / conflicting item along with full chunk text."""
    from ..database import SessionLocal

    async with SessionLocal() as db:
        items = (
            await db.execute(
                select(ScopeItem)
                .where(ScopeItem.run_id == run_id)
                .where(
                    or_(
                        ScopeItem.confidence < _FLAGGED_CONFIDENCE_THRESHOLD,
                        ScopeItem.qty_confidence == "conflicting",
                    )
                )
            )
        ).scalars().all()

        if not items:
            return []

        item_ids = [i.id for i in items]
        citations = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_(item_ids)
                )
            )
        ).scalars().all()
        citations_by_item: dict[str, list[ScopeCitation]] = {}
        chunk_ids: set[str] = set()
        for c in citations:
            citations_by_item.setdefault(c.scope_item_id, []).append(c)
            if c.chunk_id:
                chunk_ids.add(c.chunk_id)

        chunks_by_id: dict[str, Chunk] = {}
        if chunk_ids:
            chunk_rows = (
                await db.execute(
                    select(Chunk).where(Chunk.id.in_(chunk_ids))
                )
            ).scalars().all()
            chunks_by_id = {c.id: c for c in chunk_rows}

    flagged: list[_FlaggedItem] = []
    for it in items:
        cits = citations_by_item.get(it.id, [])
        chunks = [chunks_by_id[c.chunk_id] for c in cits if c.chunk_id in chunks_by_id]
        reasons = []
        if it.confidence < _FLAGGED_CONFIDENCE_THRESHOLD:
            reasons.append(f"validator confidence {it.confidence:.2f} < 0.60")
        if it.qty_confidence == "conflicting":
            reasons.append("quantity sources disagreed")
        flagged.append(
            _FlaggedItem(
                item=it,
                chunks=chunks,
                citations=cits,
                flag_reason="; ".join(reasons),
            )
        )
    return flagged


async def verify_low_confidence(run_id: str) -> tuple[int, int, int, float]:
    """Stage F: Opus reflection on flagged items.

    Returns: (kept, revised, rejected, total_cost_usd).
    Items with verdict='keep' get verifier_status='keep'.
    Items with verdict='revised' have description/qty/unit overwritten.
    Items with verdict='rejected' get verifier_status='rejected' but are
    NOT deleted — keep them in the audit trail; the UI hides by default.
    """
    client = _get_client()
    if client is None:
        log.warning("scope_verifier: no ANTHROPIC_API_KEY — skipping")
        return 0, 0, 0, 0.0

    flagged = await _gather_flagged(run_id)
    if not flagged:
        log.info("scope_verifier: no flagged items in run %s", run_id)
        return 0, 0, 0, 0.0

    log.info(
        "scope_verifier: Opus pass on %d flagged items in run %s",
        len(flagged),
        run_id,
    )

    sem = asyncio.Semaphore(_VERIFIER_CONCURRENCY)
    results = await asyncio.gather(
        *(_verify_one(client, flagged[0].item.project_id, f, sem) for f in flagged),
        return_exceptions=True,
    )

    from ..database import SessionLocal

    kept = revised = rejected = 0
    total_cost = 0.0
    async with SessionLocal() as db:
        for r in results:
            if isinstance(r, Exception):
                log.warning("scope_verifier: verification failed: %s", r)
                continue
            item, payload, usage, latency_ms = r

            cost, _ = await record_call(
                db,
                purpose="scope-verify",
                model=_VERIFIER_MODEL,
                usage=usage,
                latency_ms=latency_ms,
                project_id=item.project_id,
            )
            total_cost += cost or 0.0

            if payload is None:
                continue

            verdict = (payload.get("verdict") or "keep").lower()
            review_blob = {
                "verdict": verdict,
                "reasoning": payload.get("reasoning"),
                "consistency_check": payload.get("consistency_check"),
                "model": _VERIFIER_MODEL,
                "latency_ms": latency_ms,
                "cost_usd": cost,
            }

            # Re-fetch to attach to the active session
            db_item = await db.get(ScopeItem, item.id)
            if db_item is None:
                continue
            db_item.verifier_status = verdict
            db_item.verifier_review = review_blob

            if verdict == "revised":
                if payload.get("revised_description"):
                    db_item.description = payload["revised_description"]
                if payload.get("revised_quantity") is not None:
                    db_item.quantity = str(payload["revised_quantity"])
                if payload.get("revised_unit") is not None:
                    db_item.unit = payload["revised_unit"]
                # Bump confidence: Opus revised it, we trust the revised version
                db_item.confidence = max(db_item.confidence, 0.85)
                revised += 1
            elif verdict == "rejected":
                rejected += 1
            else:
                kept += 1

        await db.commit()

    log.info(
        "scope_verifier: run %s — %d kept, %d revised, %d rejected, $%.3f",
        run_id,
        kept,
        revised,
        rejected,
        total_cost,
    )
    return kept, revised, rejected, total_cost
