"""Smoke test for section_extractor.py — runs against 3 sections covering
different item types, prints what gets emitted.

Run:
  cd backend
  .venv/bin/python -m scripts.test_section_extractor
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Document
from app.services.section_extractor import extract_section
from app.services.trade_list_parser import (
    CSISection, get_taxonomy_for_project,
)


# Picked to cover spec_only (admin), bilateral (material), bilateral (equipment).
_TEST_SECTION_CODES = [
    "01 33 00",   # Submittal Procedures — should be admin / spec_only
    "03 30 00",   # Cast-In-Place Concrete — material / bilateral
    "08 14 16",   # Flush Wood Doors — equipment / bilateral
    "08 71 00",   # Door Hardware — equipment / bilateral
    "01 77 00",   # Closeout Procedures — admin / spec_only
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
        print("no taxonomy for project")
        return 1

    print(f"project: {project_id}\n")

    total_items = 0
    total_cost = 0.0
    for code in _TEST_SECTION_CODES:
        section = taxonomy.get_section(code)
        if section is None:
            section = CSISection(code=code, title="(not in taxonomy)",
                                  division_code=code[:2])
        async with SessionLocal() as db:
            result = await extract_section(db, project_id, section)

        print(f"=== {code}  {section.title} ===")
        print(f"  chunks_used={result.chunks_used}  items={len(result.items)}  "
              f"cost=${result.cost_usd:.4f}  latency={result.latency_ms}ms")
        if result.notes:
            print(f"  notes: {result.notes}")
        type_counts = Counter(
            (it.extraction_method or "").split("/")[-1] for it in result.items
        )
        print(f"  item_type breakdown: {dict(type_counts)}")
        for it in result.items[:8]:
            print(f"    [{it.csi_code}] {it.description[:80]}")
            print(f"      qty={it.quantity} unit={it.unit} type={(it.extraction_method or '').split('/')[-1]}")
        if len(result.items) > 8:
            print(f"    ... ({len(result.items) - 8} more)")
        print()
        total_items += len(result.items)
        total_cost += result.cost_usd

    print(f"\nTOTAL: {total_items} items across {len(_TEST_SECTION_CODES)} sections, ${total_cost:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
