"""Re-run the smart schedule miner against the live project and dump every
candidate so we can spot-check unit/CSI/qty assignments.

Run:
  cd backend
  .venv/bin/python -m scripts.run_smart_miner
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Document
from app.services.schedule_miner import mine_schedules
from app.services.trade_list_parser import get_taxonomy_for_project


async def _resolve_project_id() -> str:
    """Pick the project that has a drawing-set document (the live test project)."""
    async with SessionLocal() as db:
        d = (
            await db.execute(
                select(Document)
                .where(Document.doc_type == "drawing-set")
                .order_by(Document.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if d is None:
            raise SystemExit("no drawing-set document found")
        return d.project_id


async def main() -> int:
    project_id = await _resolve_project_id()
    print(f"project: {project_id}\n")

    taxonomy = await get_taxonomy_for_project(project_id)
    if taxonomy is None:
        raise SystemExit("no taxonomy for project")

    by_division, cost = await mine_schedules(project_id, taxonomy)
    total = sum(len(c) for c in by_division.values())
    print(f"total candidates: {total}    cost: ${cost:.4f}\n")

    rows: list[dict] = []
    unit_count: Counter[str] = Counter()
    div_count: Counter[str] = Counter()
    for div, cands in sorted(by_division.items()):
        for c in cands:
            row_text = ""
            if c.supporting_chunks:
                row_text = (c.supporting_chunks[0].chunk.text or "")[:160]
            rows.append({
                "division": div,
                "csi_code": c.csi_code,
                "description": c.description,
                "qty": c.quantity,
                "unit": c.unit,
                "specification": c.specification,
                "row_text": row_text,
            })
            unit_count[c.unit or ""] += 1
            div_count[div] += 1

    out_path = Path("data/outputs/smart_miner_run.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2, default=str))

    print(f"per-division counts (n={len(rows)}):")
    for div, n in sorted(div_count.items()):
        print(f"  {div}: {n}")
    print()
    print("unit distribution:")
    for u, n in sorted(unit_count.items(), key=lambda x: -x[1]):
        print(f"  {u or '(none)'}: {n}")
    print()
    print(f"full dump: {out_path}")

    print("\n" + "=" * 100)
    print("FULL ROW LIST")
    print("=" * 100)
    print(f"{'#':<4} {'div':<5} {'csi':<10} {'unit':<5} {'qty':<8}  description")
    print("-" * 110)
    for i, r in enumerate(rows, 1):
        desc = (r['description'] or "")[:70]
        print(f"{i:<4} {r['division']:<5} {(r['csi_code'] or '')[:10]:<10} "
              f"{(r['unit'] or '')[:5]:<5} {(r['qty'] or '')[:8]:<8}  {desc}")
        if r['row_text']:
            print(f"      row: {r['row_text'][:90]}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
