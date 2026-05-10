"""API schemas for RAGAS evaluation runs."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RagasFixtureOut(BaseModel):
    csi_section: str
    section_title: str
    n_extracted_items: int
    n_expected_items: int
    n_retrieved_chunks: int
    context_precision: float
    context_recall: float
    answer_faithfulness: float
    answer_relevancy: float
    cost_usd: float


class RagasEvalRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    scope_run_id: str

    status: str
    started_at: datetime
    completed_at: datetime | None
    error: str | None

    fixtures_path: str
    n_fixtures: int

    context_precision: float | None
    context_recall: float | None
    answer_faithfulness: float | None
    answer_relevancy: float | None
    overall_score: float | None

    per_fixture: list[RagasFixtureOut] | None

    total_cost_usd: float
    elapsed_sec: float
