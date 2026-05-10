"""API schemas for ProjectProfile + TradeDivisionRelevance."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProjectProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str

    building_type: str | None
    size_sf: float | None
    occupancy: str | None
    construction_type: str | None
    sprinklered: bool | None
    stories: int | None
    location: str | None
    project_number: str | None
    codes: list[str] | None
    reasoning: str | None

    model: str | None
    cost_usd: float | None
    latency_ms: int | None

    created_at: datetime
    updated_at: datetime


class TradeDivisionRelevanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    csi_division: str
    division_label: str
    is_relevant: bool
    reasoning: str | None
    confidence: float | None
    operator_override: bool
    override_value: bool | None
    cost_usd: float | None
    latency_ms: int | None
    created_at: datetime


class TradeRelevanceOverrideIn(BaseModel):
    """Operator override request body."""

    csi_division: str
    is_relevant: bool


class TradeRelevanceMatrix(BaseModel):
    """Project-wide relevance summary — feeds the chip-cloud UI."""

    project_id: str
    relevant_count: int
    skipped_count: int
    total_cost_usd: float
    divisions: list[TradeDivisionRelevanceOut]
