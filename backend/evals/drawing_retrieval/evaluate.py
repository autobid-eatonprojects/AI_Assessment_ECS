"""Evaluate drawing retrieval recall on construction drawings.

For each query, runs hybrid_retrieve at top_k=20 and asks:
  - Did the retrieved chunks come from any of the ground-truth sheet IDs?

A "hit" means the chunk's sheet_number (in chunk.extra) matches one of the
relevant_sheets for that query. Recall@K is the fraction of relevant
sheets that have at least one chunk in top-K.

Run:
  cd backend
  .venv/bin/python -m evals.drawing_retrieval.evaluate
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import yaml
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Document
from app.services import retriever


# Hard-coded for the live Test project's drawings doc.
DRAWINGS_DOC_FILENAME_HINT = "Drawings"


async def _resolve_drawings_doc() -> tuple[str, str]:
    """Find (project_id, doc_id) for the drawing-set document."""
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
        return d.project_id, d.id


async def _evaluate_query(
    db,
    project_id: str,
    drawings_doc_id: str,
    query: str,
    relevant_sheets: set[str],
    top_k: int = 20,
) -> dict:
    candidates = await retriever.hybrid_retrieve(
        db, project_id, query, top_k=top_k
    )
    spec_candidates = [
        c for c in candidates if c.chunk.document_id == drawings_doc_id
    ]
    rank_of_first_hit: int | None = None
    sheets_covered: set[str] = set()
    rows: list[dict] = []
    for rank, rc in enumerate(spec_candidates, start=1):
        sheet = (rc.chunk.extra or {}).get("sheet_number")
        hit = sheet in relevant_sheets if sheet else False
        if hit:
            sheets_covered.add(sheet)
            if rank_of_first_hit is None:
                rank_of_first_hit = rank
        rows.append({
            "rank": rank,
            "sheet": sheet,
            "page": rc.chunk.page_number,
            "rrf": round(rc.rrf_score, 4),
            "hit": hit,
            "snippet": (rc.chunk.text or "")[:120].replace("\n", " "),
        })

    recall = (
        len(sheets_covered) / len(relevant_sheets) if relevant_sheets else None
    )
    return {
        "candidates_total": len(candidates),
        "candidates_in_drawings": len(spec_candidates),
        "sheets_covered": sorted(sheets_covered),
        "sheets_missing": sorted(relevant_sheets - sheets_covered),
        "recall": recall,
        "rank_of_first_hit": rank_of_first_hit,
        "top_5_chunks": rows[:5],
    }


async def main() -> int:
    eval_path = Path(__file__).parent / "queries.yaml"
    with eval_path.open() as f:
        eval_set = yaml.safe_load(f)

    queries = eval_set.get("queries") or []
    project_id, drawings_doc_id = await _resolve_drawings_doc()
    print(f"project: {project_id}\ndrawings doc: {drawings_doc_id}\n")

    by_difficulty: dict[str, list[dict]] = {}
    by_query: list[dict] = []

    async with SessionLocal() as db:
        for q in queries:
            result = await _evaluate_query(
                db,
                project_id,
                drawings_doc_id,
                q["text"],
                set(q.get("relevant_sheets") or []),
                top_k=20,
            )
            row = {
                "id": q["id"],
                "text": q["text"][:80],
                "difficulty": q.get("difficulty", "?"),
                "relevant": q.get("relevant_sheets") or [],
                **result,
            }
            by_query.append(row)
            by_difficulty.setdefault(q.get("difficulty", "?"), []).append(row)

    # Per-query report
    print(f"{'id':<5} {'diff':<13} {'recall':<7} {'rank':<6} {'cov/needed':<12} query")
    print("-" * 110)
    for r in by_query:
        rec = f"{r['recall']:.0%}" if r.get('recall') is not None else "  -  "
        rr = r.get('rank_of_first_hit')
        rank_str = f"#{rr}" if rr else " - "
        cov = f"{len(r['sheets_covered'])}/{len(r['relevant'])}" if r['relevant'] else "0/0"
        print(f"{r['id']:<5} {r['difficulty']:<13} {rec:<7} {rank_str:<6} {cov:<12} {r['text']}")
        if r["sheets_missing"]:
            print(f"      missing: {', '.join(r['sheets_missing'])}")

    # Aggregate
    print("\n" + "=" * 110)
    for diff in sorted(by_difficulty.keys()):
        rows = by_difficulty[diff]
        scored = [r for r in rows if r.get("recall") is not None]
        if not scored:
            print(f"{diff:<14}  {len(rows)} queries, no recall (control)")
            continue
        avg_recall = sum(r["recall"] for r in scored) / len(scored)
        with_hit = sum(1 for r in scored if (r.get("rank_of_first_hit") or 0) > 0)
        mrr = (
            sum(1.0 / r["rank_of_first_hit"] for r in scored if r.get("rank_of_first_hit"))
            / len(scored)
        )
        print(
            f"{diff:<14}  N={len(scored):>3}  recall@20={avg_recall:.1%}  "
            f"hit-rate={with_hit}/{len(scored)} ({with_hit/len(scored):.0%})  "
            f"MRR={mrr:.3f}"
        )

    scored_all = [r for r in by_query if r.get("recall") is not None]
    if scored_all:
        avg = sum(r["recall"] for r in scored_all) / len(scored_all)
        with_hit = sum(1 for r in scored_all if (r.get("rank_of_first_hit") or 0) > 0)
        mrr_all = (
            sum(1.0 / r["rank_of_first_hit"] for r in scored_all if r.get("rank_of_first_hit"))
            / len(scored_all)
        )
        print(
            f"{'OVERALL':<14}  N={len(scored_all):>3}  recall@20={avg:.1%}  "
            f"hit-rate={with_hit}/{len(scored_all)} ({with_hit/len(scored_all):.0%})  "
            f"MRR={mrr_all:.3f}"
        )

    out_path = Path(__file__).parent / "results_baseline.json"
    out_path.write_text(json.dumps({"queries": by_query}, indent=2, default=str))
    print(f"\nresults dumped to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
