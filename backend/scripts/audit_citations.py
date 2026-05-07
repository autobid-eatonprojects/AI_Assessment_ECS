"""Replay the L2 citation validator over the persisted citations of a run
to measure what it WOULD have dropped if applied during persistence.

Run:
  cd backend
  .venv/bin/python -m scripts.audit_citations
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter, defaultdict

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Chunk, ScopeCitation, ScopeItem
from app.services.citation_validator import validate_citation


async def main() -> int:
    async with SessionLocal() as db:
        # Find the most recent run
        latest = (
            await db.execute(
                select(ScopeItem.run_id, ScopeItem.project_id)
                .order_by(ScopeItem.created_at.desc())
                .limit(1)
            )
        ).first()
        if latest is None:
            print("no scope items found")
            return 1
        run_id, project_id = latest
        print(f"run: {run_id}\nproject: {project_id}\n")

        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        item_by_id = {i.id: i for i in items}

        cits = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_([i.id for i in items])
                )
            )
        ).scalars().all()

        chunk_ids = {c.chunk_id for c in cits}
        chunks = (
            await db.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
        ).scalars().all()
        chunk_by_id = {c.id: c for c in chunks}

    # Replay
    by_reason: Counter[str] = Counter()
    by_eviden: dict[str, Counter[str]] = defaultdict(Counter)
    by_method: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[tuple]] = defaultdict(list)
    items_left_with_no_citation: set[str] = set()
    items_now_unilateral: set[str] = set()

    # First pass: tally
    survivors_per_item: dict[str, list[str]] = defaultdict(list)
    for c in cits:
        item = item_by_id.get(c.scope_item_id)
        chunk = chunk_by_id.get(c.chunk_id)
        if item is None:
            continue
        chunk_text = (chunk.text if chunk else None) or c.excerpt or ""
        v = validate_citation(item.description or "", chunk_text)
        method = item.extraction_method or "?"
        if v.valid:
            survivors_per_item[item.id].append(c.evidence_type or "?")
        else:
            by_reason[v.reason] += 1
            by_eviden[v.reason][c.evidence_type or "?"] += 1
            by_method[v.reason][method] += 1
            if len(examples[v.reason]) < 5:
                examples[v.reason].append((
                    item.description[:60],
                    chunk_text[:80].replace("\n", " "),
                    v.overlap,
                ))

    # Second pass — items that would lose ALL citations / become unilateral
    item_evidence_before: dict[str, set[str]] = defaultdict(set)
    for c in cits:
        item_evidence_before[c.scope_item_id].add(c.evidence_type or "?")

    for item_id, item in item_by_id.items():
        before = item_evidence_before.get(item_id, set())
        after = set(survivors_per_item.get(item_id, []))
        if before and not after:
            items_left_with_no_citation.add(item_id)
            continue
        # bilateral before = had spec AND drawing
        # bilateral after  = same after dropping
        was_bilateral = "spec" in before and "drawing" in before
        is_bilateral_now = "spec" in after and "drawing" in after
        if was_bilateral and not is_bilateral_now:
            items_now_unilateral.add(item_id)

    print(f"=== L2 audit on run {run_id} ===")
    print(f"items={len(items)}  citations={len(cits)}")
    print(f"\nValidator would drop {sum(by_reason.values())} citations:")
    for reason, n in by_reason.most_common():
        print(f"  {reason:<13} {n}")
        print(f"    by evidence_type: {dict(by_eviden[reason])}")
        print(f"    by extraction_method: {dict(by_method[reason])}")

    print(f"\nItems left with NO citations: {len(items_left_with_no_citation)}")
    print(f"Items demoted from bilateral → unilateral: {len(items_now_unilateral)}")

    print(f"\n=== Sample rejections per reason ===")
    for reason, ex in examples.items():
        print(f"\n{reason}:")
        for desc, chunk, ov in ex:
            print(f"  [overlap={ov:.2f}] desc={desc!r}")
            print(f"                  chunk={chunk!r}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
