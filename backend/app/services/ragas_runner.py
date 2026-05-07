"""Background runner that persists RAGAS evaluations.

Wraps `ragas_eval.evaluate_run` with a RagasEvalRun row that the UI can
poll. Same fire-and-forget pattern as scope_runner: the API enqueues a
task, returns the row id, and the frontend polls until status='complete'.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..database import SessionLocal
from ..models import RagasEvalRun
from .ragas_eval import evaluate_run, load_fixtures_from_yaml

log = logging.getLogger(__name__)


_DEFAULT_FIXTURES = (
    Path(__file__).resolve().parent.parent.parent / "evals" / "elks_ground_truth.yaml"
)


_inflight: set[asyncio.Task] = set()


def schedule_ragas_eval(eval_run_id: str, fixtures_path: str) -> asyncio.Task:
    """Fire-and-forget. The eval row already exists in 'running' state."""
    task = asyncio.create_task(_run_with_failure_capture(eval_run_id, fixtures_path))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return task


async def _run_with_failure_capture(eval_run_id: str, fixtures_path: str) -> None:
    try:
        await _execute(eval_run_id, fixtures_path)
    except Exception as e:  # noqa: BLE001
        log.exception("ragas_runner: eval %s failed", eval_run_id)
        async with SessionLocal() as db:
            row = await db.get(RagasEvalRun, eval_run_id)
            if row is not None:
                row.status = "failed"
                row.error = f"{type(e).__name__}: {e}"[:1000]
                row.completed_at = datetime.now(timezone.utc)
                await db.commit()


async def _execute(eval_run_id: str, fixtures_path: str) -> None:
    fixtures = load_fixtures_from_yaml(fixtures_path)
    async with SessionLocal() as db:
        row = await db.get(RagasEvalRun, eval_run_id)
        if row is None:
            log.warning("ragas_runner: eval row %s vanished — bailing", eval_run_id)
            return
        scope_run_id = row.scope_run_id
        row.n_fixtures = len(fixtures)
        await db.commit()

    result = await evaluate_run(scope_run_id, fixtures)

    async with SessionLocal() as db:
        row = await db.get(RagasEvalRun, eval_run_id)
        if row is None:
            return
        row.status = "complete"
        row.completed_at = datetime.now(timezone.utc)
        row.context_precision = result.context_precision
        row.context_recall = result.context_recall
        row.answer_faithfulness = result.answer_faithfulness
        row.answer_relevancy = result.answer_relevancy
        row.overall_score = result.overall_score
        row.per_fixture = [
            {
                "csi_section": f.csi_section,
                "section_title": f.section_title,
                "n_extracted_items": f.n_extracted_items,
                "n_expected_items": f.n_expected_items,
                "n_retrieved_chunks": f.n_retrieved_chunks,
                "context_precision": f.context_precision,
                "context_recall": f.context_recall,
                "answer_faithfulness": f.answer_faithfulness,
                "answer_relevancy": f.answer_relevancy,
                "cost_usd": f.cost_usd,
            }
            for f in result.per_fixture
        ]
        row.total_cost_usd = result.total_cost_usd
        row.elapsed_sec = result.elapsed_sec
        await db.commit()

    log.info(
        "ragas_runner: eval %s complete — overall=%.1f%% in %.1fs ($%.4f)",
        eval_run_id,
        result.overall_score * 100,
        result.elapsed_sec,
        result.total_cost_usd,
    )


def default_fixtures_path() -> str:
    return str(_DEFAULT_FIXTURES)
