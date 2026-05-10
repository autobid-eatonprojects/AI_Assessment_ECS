"""Compare two ScopeExtractionRun rows side by side.

Usage:
  cd backend
  PYTHONPATH=. python scripts/compare_runs.py <run_id_a> <run_id_b>

Reads both runs by id and produces a markdown report saved to
`backend/data/run_compare_{a8}_{b8}.md`. Use this to validate that a
new orchestration mode (e.g. per-section) doesn't drop items the old
mode (per-discipline) produced — the "didn't regress" gate before
flipping the default.

The report covers:
  - Total ScopeItem count (A → B), delta, % change
  - Cost (A → B)
  - Per-division item count diff (flags any division where B < 0.5×A)
  - Per-CSI-section item count diff (top 30)
  - Items in A but missing in B (matched by (csi_code, normalized desc)) —
    suspected drops
  - Items in B but new vs A — what the new mode discovered
  - Citation counts (spec / drawing / total)
  - Gap counts by type
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Gap,
    ScopeCitation,
    ScopeExtractionRun,
    ScopeItem,
)


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _key(item: ScopeItem) -> tuple[str, str]:
    """Stable identity for matching items across runs."""
    return (item.csi_code or "", _norm(item.description)[:120])


async def _load(run_id: str) -> dict:
    async with SessionLocal() as db:
        run = await db.get(ScopeExtractionRun, run_id)
        if run is None:
            raise SystemExit(f"run {run_id} not found")
        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        cit_counts = (
            await db.execute(
                select(ScopeCitation.evidence_type, func.count(ScopeCitation.id))
                .join(ScopeItem, ScopeCitation.scope_item_id == ScopeItem.id)
                .where(ScopeItem.run_id == run_id)
                .group_by(ScopeCitation.evidence_type)
            )
        ).all()
        gap_counts = (
            await db.execute(
                select(Gap.gap_type, func.count(Gap.id))
                .where(Gap.run_id == run_id)
                .group_by(Gap.gap_type)
            )
        ).all()
        return {
            "run": run,
            "items": items,
            "by_division": Counter(i.csi_division for i in items),
            "by_section": Counter(i.csi_code for i in items),
            "by_method": Counter(i.extraction_method for i in items),
            "keys": {_key(i): i for i in items},
            "citation_by_type": {t: int(n) for t, n in cit_counts},
            "gap_by_type": {t: int(n) for t, n in gap_counts},
        }


def _delta_pct(a: int, b: int) -> str:
    if a == 0:
        return f"+{b}" if b else "0"
    pct = (b - a) / a * 100
    sign = "+" if pct >= 0 else ""
    return f"{b - a:+d} ({sign}{pct:.1f}%)"


def _render(a: dict, b: dict) -> str:
    a_run = a["run"]
    b_run = b["run"]
    a_items, b_items = a["items"], b["items"]
    a_keys, b_keys = a["keys"], b["keys"]

    only_a = [a_keys[k] for k in a_keys.keys() - b_keys.keys()]
    only_b = [b_keys[k] for k in b_keys.keys() - a_keys.keys()]

    lines: list[str] = []
    lines.append(f"# Run comparison\n")
    lines.append(f"- **A**: `{a_run.id}` mode={a_run.config.get('mode') if a_run.config else '?'} started={a_run.started_at}")
    lines.append(f"- **B**: `{b_run.id}` mode={b_run.config.get('mode') if b_run.config else '?'} started={b_run.started_at}\n")

    lines.append("## Top-line\n")
    lines.append("| Metric | A | B | Δ |")
    lines.append("|---|---:|---:|---:|")
    lines.append(f"| ScopeItems | {len(a_items)} | {len(b_items)} | {_delta_pct(len(a_items), len(b_items))} |")
    lines.append(f"| Distinct divisions | {len(a['by_division'])} | {len(b['by_division'])} | — |")
    lines.append(f"| Distinct sections | {len(a['by_section'])} | {len(b['by_section'])} | — |")
    lines.append(f"| Items only in A (drops) | — | — | **{len(only_a)}** |")
    lines.append(f"| Items only in B (new) | — | — | **{len(only_b)}** |\n")

    lines.append("## Per-division\n")
    lines.append("| Div | A | B | Δ | Flag |")
    lines.append("|---|---:|---:|---:|---|")
    all_divs = sorted(set(a["by_division"]) | set(b["by_division"]))
    for d in all_divs:
        na, nb = a["by_division"][d], b["by_division"][d]
        flag = ""
        if na > 0 and nb < 0.5 * na:
            flag = "⚠ regression"
        elif nb > 0 and na == 0:
            flag = "✓ new coverage"
        lines.append(f"| {d} | {na} | {nb} | {_delta_pct(na, nb)} | {flag} |")
    lines.append("")

    lines.append("## Per-section (top 30 by combined count)\n")
    combined = Counter(a["by_section"]) + Counter(b["by_section"])
    top = [s for s, _ in combined.most_common(30)]
    lines.append("| Section | A | B | Δ |")
    lines.append("|---|---:|---:|---:|")
    for s in top:
        na, nb = a["by_section"][s], b["by_section"][s]
        lines.append(f"| {s} | {na} | {nb} | {_delta_pct(na, nb)} |")
    lines.append("")

    lines.append("## Citations by evidence type\n")
    et_keys = sorted(set(a["citation_by_type"]) | set(b["citation_by_type"]))
    lines.append("| Type | A | B |")
    lines.append("|---|---:|---:|")
    for et in et_keys:
        lines.append(f"| {et} | {a['citation_by_type'].get(et, 0)} | {b['citation_by_type'].get(et, 0)} |")
    lines.append("")

    lines.append("## Gaps by type\n")
    gt_keys = sorted(set(a["gap_by_type"]) | set(b["gap_by_type"]))
    lines.append("| Type | A | B | Δ |")
    lines.append("|---|---:|---:|---:|")
    for gt in gt_keys:
        na, nb = a["gap_by_type"].get(gt, 0), b["gap_by_type"].get(gt, 0)
        lines.append(f"| {gt} | {na} | {nb} | {_delta_pct(na, nb)} |")
    lines.append("")

    if only_a:
        lines.append(f"## Items in A but not in B ({len(only_a)} potential drops)\n")
        lines.append("Top 25 by csi_code:\n")
        only_a_sorted = sorted(only_a, key=lambda i: (i.csi_code or "", _norm(i.description)))
        for i in only_a_sorted[:25]:
            lines.append(f"- `{i.csi_code}` — {(i.description or '')[:140]}")
        if len(only_a) > 25:
            lines.append(f"\n_(…and {len(only_a) - 25} more)_")
        lines.append("")

    if only_b:
        lines.append(f"## Items in B but not in A ({len(only_b)} new)\n")
        lines.append("Top 25 by csi_code:\n")
        only_b_sorted = sorted(only_b, key=lambda i: (i.csi_code or "", _norm(i.description)))
        for i in only_b_sorted[:25]:
            lines.append(f"- `{i.csi_code}` — {(i.description or '')[:140]}")
        if len(only_b) > 25:
            lines.append(f"\n_(…and {len(only_b) - 25} more)_")
        lines.append("")

    lines.append("## Verdict\n")
    if len(b_items) < 0.85 * len(a_items):
        lines.append(f"⚠ B has {len(b_items)} items vs A's {len(a_items)} (>15% drop). Investigate before flipping default.")
    elif len(only_a) > 0.20 * len(a_items):
        lines.append(f"⚠ {len(only_a)} items present in A but missing from B (>20% of A). Investigate.")
    else:
        lines.append(f"✓ B has {len(b_items)} items, no major drop. {len(only_a)} items in A→missing, {len(only_b)} new in B.")
    return "\n".join(lines) + "\n"


async def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python scripts/compare_runs.py <run_id_a> <run_id_b>", file=sys.stderr)
        sys.exit(2)
    a_id, b_id = sys.argv[1], sys.argv[2]
    a, b = await asyncio.gather(_load(a_id), _load(b_id))
    report = _render(a, b)

    out_dir = Path("data")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"run_compare_{a_id[:8]}_{b_id[:8]}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"wrote {out_path}")
    # Also stream the verdict line to stdout so CI/loops can grep it
    for ln in report.splitlines():
        if ln.startswith(("✓", "⚠")):
            print(ln)


if __name__ == "__main__":
    asyncio.run(main())
