"""Evaluate the current chunker+retriever's recall on construction spec queries.

For each query in queries.yaml, runs hybrid_retrieve at top_k=20 and asks:
  - Did the retrieved chunks come from a page that's WITHIN any of the
    relevant CSI sections' page ranges?

A "hit" means the chunk's page lies between [section_start, next_section_start)
for one of the ground-truth CSI codes. This is a coarse but defensible
proxy for "the retriever surfaced the right section" — we can't ask
chunk-level "does this answer the question?" without LLM-as-judge.

Metrics:
  - recall@K: fraction of relevant CSI sections covered by top-K chunks
  - mrr: mean reciprocal rank of FIRST hit per query (only over queries
         that have any relevant CSI codes)
  - per-query breakdown so you can see which queries fail

Run:
  cd backend
  .venv/bin/python -m evals.spec_retrieval.evaluate
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import yaml
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Chunk, Document, DocumentPage
from app.services import retriever


# Hard-coded for the live Test project's Project Manual. To run on a different
# project, point this at the right doc id.
SPEC_DOC_ID = "4dc9fb06-8d39-4ad0-81b6-fabeab8b869f"


_SECTION_HEADER = re.compile(
    r"(?:^|\n)\s*SECTION\s+(\d{2}\s+\d{2}\s+\d{2})", re.IGNORECASE | re.MULTILINE
)


async def _build_csi_page_ranges(spec_doc_id: str) -> dict[str, tuple[int, int]]:
    """Scan the spec book; return {csi_code: (start_page, end_page)}."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(DocumentPage.page_number, DocumentPage.text_content)
                .where(DocumentPage.document_id == spec_doc_id)
                .order_by(DocumentPage.page_number)
            )
        ).all()

    section_starts: list[tuple[str, int]] = []
    seen: set[str] = set()
    for pn, txt in rows:
        if not txt:
            continue
        for m in _SECTION_HEADER.finditer(txt):
            code = m.group(1).strip()
            if code not in seen:
                section_starts.append((code, pn))
                seen.add(code)

    section_starts.sort(key=lambda x: x[1])
    last_page = max(pn for pn, _ in rows)

    ranges: dict[str, tuple[int, int]] = {}
    for i, (code, start) in enumerate(section_starts):
        end = section_starts[i + 1][1] - 1 if i + 1 < len(section_starts) else last_page
        ranges[code] = (start, end)
    return ranges


async def _resolve_project_id_for_doc(doc_id: str) -> str:
    async with SessionLocal() as db:
        d = await db.get(Document, doc_id)
        if d is None:
            raise SystemExit(f"document not found: {doc_id}")
        return d.project_id


def _hit_csi_for_chunk(
    chunk: Chunk, ranges: dict[str, tuple[int, int]]
) -> set[str]:
    """Return the CSI codes whose page range contains this chunk."""
    page = chunk.page_number
    if page is None:
        return set()
    out: set[str] = set()
    for code, (start, end) in ranges.items():
        if start <= page <= end:
            out.add(code)
    return out


async def _evaluate_query(
    db,
    project_id: str,
    spec_doc_id: str,
    query: str,
    relevant_csi: set[str],
    page_ranges: dict[str, tuple[int, int]],
    top_k: int = 20,
) -> dict:
    candidates = await retriever.hybrid_retrieve(
        db, project_id, query, top_k=top_k
    )
    # Filter to chunks from the spec doc
    spec_candidates = [c for c in candidates if c.chunk.document_id == spec_doc_id]
    rank_of_first_hit: int | None = None
    csi_covered: set[str] = set()
    rows: list[dict] = []
    for rank, rc in enumerate(spec_candidates, start=1):
        hit_codes = _hit_csi_for_chunk(rc.chunk, page_ranges) & relevant_csi
        if hit_codes and rank_of_first_hit is None:
            rank_of_first_hit = rank
        csi_covered |= hit_codes
        rows.append({
            "rank": rank,
            "page": rc.chunk.page_number,
            "rrf": round(rc.rrf_score, 4),
            "hit": sorted(hit_codes),
            "snippet": (rc.chunk.text or "")[:120].replace("\n", " "),
        })

    # Recall — fraction of relevant CSI sections covered (only meaningful
    # when relevant_csi is non-empty)
    recall = (
        len(csi_covered) / len(relevant_csi) if relevant_csi else None
    )

    # No-match queries: a "good" result is retrieving NOTHING from the
    # relevant_csi sections (because they don't exist for this query).
    # We can't easily score that — just report what was retrieved.

    return {
        "candidates_total": len(candidates),
        "candidates_in_spec": len(spec_candidates),
        "csi_covered": sorted(csi_covered),
        "csi_missing": sorted(relevant_csi - csi_covered),
        "recall": recall,
        "rank_of_first_hit": rank_of_first_hit,
        "top_5_chunks": rows[:5],
    }


async def main() -> int:
    eval_path = Path(__file__).parent / "queries.yaml"
    with eval_path.open() as f:
        eval_set = yaml.safe_load(f)

    queries = eval_set.get("queries") or []
    project_id = await _resolve_project_id_for_doc(SPEC_DOC_ID)
    print(f"project: {project_id}\nspec doc: {SPEC_DOC_ID}\n")

    print("Building CSI section page ranges from spec book...")
    page_ranges = await _build_csi_page_ranges(SPEC_DOC_ID)
    print(f"  {len(page_ranges)} sections detected\n")

    by_difficulty: dict[str, list[dict]] = {}
    by_query: list[dict] = []

    async with SessionLocal() as db:
        for q in queries:
            result = await _evaluate_query(
                db,
                project_id,
                SPEC_DOC_ID,
                q["text"],
                set(q.get("relevant_csi") or []),
                page_ranges,
            )
            row = {
                "id": q["id"],
                "text": q["text"][:80],
                "difficulty": q.get("difficulty", "?"),
                "relevant": q.get("relevant_csi") or [],
                **result,
            }
            by_query.append(row)
            by_difficulty.setdefault(q.get("difficulty", "?"), []).append(row)

    # Per-query report
    print(f"{'id':<5} {'diff':<10} {'recall':<7} {'mrr':<6} {'covered/needed':<22} query")
    print("-" * 110)
    for r in by_query:
        rec = f"{r['recall']:.0%}" if r.get('recall') is not None else "  -  "
        rr = r.get('rank_of_first_hit')
        mrr = f"1/{rr}" if rr else " - "
        covered = f"{len(r['csi_covered'])}/{len(r['relevant'])}" if r['relevant'] else "0/0"
        print(f"{r['id']:<5} {r['difficulty']:<10} {rec:<7} {mrr:<6} {covered:<22} {r['text']}")
        if r["csi_missing"]:
            print(f"      missing: {', '.join(r['csi_missing'])}")

    # Aggregate
    print("\n" + "=" * 110)
    for diff, rows in by_difficulty.items():
        scored = [r for r in rows if r.get("recall") is not None]
        if not scored:
            print(f"{diff:<12}  {len(rows)} queries, no recall (control)")
            continue
        avg_recall = sum(r["recall"] for r in scored) / len(scored)
        with_hit = sum(1 for r in scored if (r.get("rank_of_first_hit") or 0) > 0)
        mrr = (
            sum(1.0 / r["rank_of_first_hit"] for r in scored if r.get("rank_of_first_hit"))
            / len(scored)
            if scored else 0.0
        )
        print(
            f"{diff:<12}  N={len(scored):>3}  recall@20={avg_recall:.1%}  "
            f"hit-rate={with_hit}/{len(scored)} ({with_hit/len(scored):.0%})  "
            f"MRR={mrr:.3f}"
        )

    # Overall
    scored_all = [r for r in by_query if r.get("recall") is not None]
    if scored_all:
        avg = sum(r["recall"] for r in scored_all) / len(scored_all)
        with_hit = sum(1 for r in scored_all if (r.get("rank_of_first_hit") or 0) > 0)
        mrr_all = (
            sum(1.0 / r["rank_of_first_hit"] for r in scored_all if r.get("rank_of_first_hit"))
            / len(scored_all)
        )
        print(
            f"{'OVERALL':<12}  N={len(scored_all):>3}  recall@20={avg:.1%}  "
            f"hit-rate={with_hit}/{len(scored_all)} ({with_hit/len(scored_all):.0%})  "
            f"MRR={mrr_all:.3f}"
        )

    # Dump JSON for diff'ing across runs
    out = {"page_ranges": page_ranges, "queries": by_query}
    out_path = Path(__file__).parent / "results_baseline.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nresults dumped to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
