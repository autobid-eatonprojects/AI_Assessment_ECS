"""Smoke test for drawing_grounder.py — runs section_extractor on a handful
of material/equipment-heavy sections, then runs drawing_grounder against
the resulting candidates and prints what got grounded vs unfound.

Run:
  cd backend
  .venv/bin/python -m scripts.test_drawing_grounder
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Document
from app.services.drawing_grounder import ground_items
from app.services.section_extractor import extract_section
from app.services.trade_list_parser import (
    CSISection, get_taxonomy_for_project,
)


# Sections likely to have plenty of drawing-side presence:
_TEST_SECTION_CODES = [
    "08 14 16",   # Wood Doors — should appear on A1.1, A2.1
    "22 41 00",   # Plumbing Fixtures — should appear on P1.1/P2.1
    "26 51 00",   # Lighting — should appear on E3.1
    "23 73 00",   # Air-Handling Units (if exists) or any 23 — M1.1
]


async def main() -> int:
    async with SessionLocal() as db:
        d = (await db.execute(
            select(Document).where(Document.doc_type == "drawing-set")
            .order_by(Document.created_at.desc()).limit(1)
        )).scalar_one()
        project_id = d.project_id

    taxonomy = await get_taxonomy_for_project(project_id)
    if taxonomy is None:
        print("no taxonomy"); return 1

    # Stage A — section_extractor across a few sections
    items = []
    for code in _TEST_SECTION_CODES:
        section = taxonomy.get_section(code) or CSISection(
            code=code, title="(?)", division_code=code[:2]
        )
        async with SessionLocal() as db:
            result = await extract_section(db, project_id, section)
        eligible = [
            it for it in result.items
            if getattr(it, "_item_type", None) in ("material", "equipment")
        ]
        items.extend(eligible)
        print(f"section_extractor: {code} → {len(result.items)} items "
              f"({len(eligible)} non-admin)")
    print(f"\nTotal eligible items going into drawing_grounder: {len(items)}\n")

    # Stage B — drawing_grounder
    items, gr = await ground_items(project_id, items)

    print("\n" + "=" * 100)
    print(f"DRAWING GROUNDER RESULT")
    print("=" * 100)
    print(f"items_total       = {gr.items_total}")
    print(f"items_skipped     = {gr.items_skipped_admin}")
    print(f"items_grounded    = {gr.items_grounded}")
    print(f"items_unfound     = {gr.items_unfound}")
    print(f"sheets_queried    = {gr.sheets_queried}")
    print(f"latency_ms        = {gr.latency_ms}")

    print("\n--- Items WITH drawing evidence ---")
    grounded_count = 0
    for it in items:
        drw = [
            sc for sc in it.supporting_chunks
            if (sc.chunk.extra or {}).get("drawing_grounding") is True
        ]
        if not drw:
            continue
        grounded_count += 1
        print(f"\n  • {it.description[:90]}")
        for sc in drw:
            print(f"    cite: {sc.chunk.text[:160]}")

    print("\n--- Items WITHOUT drawing evidence (sample first 10) ---")
    unfound = [
        it for it in items
        if not any(
            (sc.chunk.extra or {}).get("drawing_grounding") is True
            for sc in it.supporting_chunks
        )
    ]
    for it in unfound[:10]:
        print(f"  ✗ [{it.csi_code}] {it.description[:90]}")
    if len(unfound) > 10:
        print(f"  ... ({len(unfound) - 10} more)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
