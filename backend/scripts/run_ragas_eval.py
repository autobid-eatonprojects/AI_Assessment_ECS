"""Run RAGAS evaluation against a completed scope_extraction_run.

Usage:
  cd backend
  PYTHONPATH=. python scripts/run_ragas_eval.py [run_id] [fixtures.yaml]

Args (both optional, sensible defaults):
  run_id        Run id to evaluate. Defaults to the latest 'complete' run.
  fixtures.yaml Path to a YAML fixture file. Defaults to
                evals/elks_ground_truth.yaml.

Writes a markdown report to backend/data/ragas_report_{run_id_prefix}.md
and prints the aggregate scores to stdout.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ScopeExtractionRun
from app.services.ragas_eval import (
    EvalResult,
    evaluate_run,
    load_fixtures_from_yaml,
)


_DEFAULT_FIXTURES = Path(__file__).resolve().parent.parent / "evals" / "elks_ground_truth.yaml"


async def _resolve_run_id(arg: str | None) -> str:
    if arg:
        return arg
    async with SessionLocal() as db:
        r = (
            await db.execute(
                select(ScopeExtractionRun)
                .where(ScopeExtractionRun.status == "complete")
                .order_by(ScopeExtractionRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if r is None:
            print("ERROR: no completed runs in DB", file=sys.stderr)
            sys.exit(2)
        return r.id


def _format_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _grade(v: float) -> str:
    if v >= 0.85:
        return "A"
    if v >= 0.75:
        return "B"
    if v >= 0.6:
        return "C"
    if v >= 0.4:
        return "D"
    return "F"


def _render_report(result: EvalResult, fixtures_path: Path) -> str:
    lines: list[str] = []
    lines.append(f"# RAGAS Evaluation — Run {result.run_id[:8]}")
    lines.append("")
    lines.append(f"- **Fixtures**: {fixtures_path}")
    lines.append(f"- **N fixtures evaluated**: {result.n_fixtures}")
    lines.append(f"- **Wall time**: {result.elapsed_sec:.1f}s")
    lines.append(f"- **LLM cost (Haiku judge + reverse-q)**: ${result.total_cost_usd:.4f}")
    lines.append("")

    lines.append("## Aggregate metrics (macro-average across fixtures)")
    lines.append("")
    lines.append("| Metric | Score | Grade |")
    lines.append("|---|---:|:---:|")
    lines.append(f"| context_precision | {_format_pct(result.context_precision)} | {_grade(result.context_precision)} |")
    lines.append(f"| context_recall    | {_format_pct(result.context_recall)} | {_grade(result.context_recall)} |")
    lines.append(f"| answer_faithfulness | {_format_pct(result.answer_faithfulness)} | {_grade(result.answer_faithfulness)} |")
    lines.append(f"| answer_relevancy  | {_format_pct(result.answer_relevancy)} | {_grade(result.answer_relevancy)} |")
    lines.append(f"| **overall (geom mean)** | **{_format_pct(result.overall_score)}** | **{_grade(result.overall_score)}** |")
    lines.append("")

    lines.append("## Per-fixture detail")
    lines.append("")
    lines.append("| Section | Title | extracted | expected | retrieved | precision | recall | faith. | relev. |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for f in result.per_fixture:
        lines.append(
            f"| `{f.csi_section}` | {f.section_title} | {f.n_extracted_items} | "
            f"{f.n_expected_items} | {f.n_retrieved_chunks} | "
            f"{_format_pct(f.context_precision)} | {_format_pct(f.context_recall)} | "
            f"{_format_pct(f.answer_faithfulness)} | {_format_pct(f.answer_relevancy)} |"
        )
    lines.append("")

    return "\n".join(lines) + "\n"


async def main() -> None:
    run_id_arg = sys.argv[1] if len(sys.argv) > 1 else None
    fixtures_path = Path(sys.argv[2]) if len(sys.argv) > 2 else _DEFAULT_FIXTURES

    if not fixtures_path.exists():
        print(f"ERROR: fixtures file not found: {fixtures_path}", file=sys.stderr)
        sys.exit(2)

    run_id = await _resolve_run_id(run_id_arg)
    fixtures = load_fixtures_from_yaml(str(fixtures_path))
    print(f"Evaluating run {run_id[:8]} against {len(fixtures)} fixtures...")
    result = await evaluate_run(run_id, fixtures)

    out_dir = Path("data")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"ragas_report_{run_id[:8]}.md"
    report = _render_report(result, fixtures_path)
    report_path.write_text(report, encoding="utf-8")

    # Stdout summary
    print()
    print(f"AGGREGATE: precision={_format_pct(result.context_precision)} "
          f"recall={_format_pct(result.context_recall)} "
          f"faith={_format_pct(result.answer_faithfulness)} "
          f"relev={_format_pct(result.answer_relevancy)} "
          f"overall={_format_pct(result.overall_score)} ({_grade(result.overall_score)})")
    print(f"cost: ${result.total_cost_usd:.4f}  wall: {result.elapsed_sec:.1f}s")
    print(f"report: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
