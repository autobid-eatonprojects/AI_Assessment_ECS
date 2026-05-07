"""Side-by-side: what the old discipline_agent emitted for each section
vs. what the new section_extractor emits. Plus a hallucination spot-check.

Run:
  cd backend
  .venv/bin/python -m scripts.compare_extractors
"""

from __future__ import annotations

import asyncio
import random
import sys
from collections import Counter

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Chunk, Document, ScopeItem
from app.services.section_extractor import extract_section
from app.services.trade_list_parser import (
    CSISection, get_taxonomy_for_project,
)


_TEST_SECTIONS = [
    "01 33 00",   # Submittals — old pipeline produced 0 items here
    "03 30 00",   # Concrete — material-heavy
    "08 14 16",   # Wood Doors — small surface
    "08 71 00",   # Door Hardware — rich (43 items)
    "01 77 00",   # Closeout — pure admin
]


async def _old_items_for_section(project_id: str, section_code: str) -> list[ScopeItem]:
    async with SessionLocal() as db:
        latest = (await db.execute(
            select(ScopeItem.run_id)
            .where(ScopeItem.project_id == project_id)
            .order_by(ScopeItem.created_at.desc()).limit(1)
        )).first()
        run_id = latest[0]
        return list((await db.execute(
            select(ScopeItem)
            .where(ScopeItem.run_id == run_id)
            .where(ScopeItem.csi_code == section_code)
        )).scalars().all())


async def main() -> int:
    async with SessionLocal() as db:
        d = (await db.execute(
            select(Document).where(Document.doc_type == "drawing-set")
            .order_by(Document.created_at.desc()).limit(1)
        )).scalar_one()
        project_id = d.project_id

    taxonomy = await get_taxonomy_for_project(project_id)
    if taxonomy is None:
        print("no taxonomy")
        return 1

    random.seed(20260507)
    spot_checks: list[tuple] = []   # (section, item_idx, item, chunks)

    for code in _TEST_SECTIONS:
        section = taxonomy.get_section(code) or CSISection(
            code=code, title="(?)", division_code=code[:2]
        )
        async with SessionLocal() as db:
            new_result = await extract_section(db, project_id, section)
        old_items = await _old_items_for_section(project_id, code)

        print("\n" + "=" * 100)
        print(f"=== {code}  {section.title} ===")
        print("=" * 100)
        print(f"OLD discipline_agent: {len(old_items)} items")
        print(f"NEW section_extractor: {len(new_result.items)} items "
              f"(${new_result.cost_usd:.4f})")

        type_counts = Counter(
            (it.extraction_method or "").split("/")[-1] for it in new_result.items
        )
        print(f"NEW item_type breakdown: {dict(type_counts)}")

        print("\n--- OLD items ---")
        if not old_items:
            print("  (none — old pipeline produced 0 for this section)")
        else:
            for it in old_items:
                print(f"  • {it.description[:90]}")

        print("\n--- NEW items ---")
        for i, it in enumerate(new_result.items):
            t = (it.extraction_method or "").split("/")[-1]
            print(f"  [{t:<8}] {it.description[:88]}")
            spot_checks.append((code, i, it, it.supporting_chunks))

    # ---- Hallucination spot-check: 4 random items, verify their cited
    # ---- spec chunks actually contain content matching the description.
    print("\n" + "=" * 100)
    print("HALLUCINATION SPOT-CHECK (4 random items)")
    print("=" * 100)
    sample = random.sample(spot_checks, min(4, len(spot_checks)))
    for section_code, idx, item, supporting in sample:
        print(f"\n>>> [{section_code} #{idx}] {item.description[:90]}")
        print(f"    cited chunks: {len(supporting)}")
        for sc in supporting[:2]:
            text = (sc.chunk.text or "")[:1000].replace("\n", " ")
            print(f"    [chunk page={sc.chunk.page_number}]: {text!r}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
