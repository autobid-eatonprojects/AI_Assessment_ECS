"""Persistence for RAGAS evaluations.

One RagasEvalRun per "Benchmark this run" click. Multiple evaluations
per scope run are kept for history (re-running with updated fixtures
or after a model upgrade). UI surfaces the most recent complete one.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RagasEvalRun(Base):
    """RAGAS benchmark of one ScopeExtractionRun against a fixture set."""

    __tablename__ = "ragas_eval_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    scope_run_id: Mapped[str] = mapped_column(
        ForeignKey("scope_extraction_runs.id", ondelete="CASCADE"), index=True
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    fixtures_path: Mapped[str] = mapped_column(String(512), nullable=False)
    n_fixtures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Macro-averaged metrics (null until status='complete')
    context_precision: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_recall: Mapped[float | None] = mapped_column(Float, nullable=True)
    answer_faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True)
    answer_relevancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Per-fixture FixtureMetrics dicts — see ragas_eval.FixtureMetrics
    per_fixture: Mapped[list | None] = mapped_column(JSON, nullable=True)

    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    elapsed_sec: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
