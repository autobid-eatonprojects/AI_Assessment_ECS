"""Schemas for vision pre-pass output.

These types are dual-purpose:
1. They define the JSON schema that Claude returns via tool use, so the
   model is constrained to produce well-formed output.
2. They are the API response shape the frontend consumes.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clamp01(v: float) -> float:
    """Tolerant clamp to [0, 1].

    Vision models occasionally return coords slightly outside [0, 1] for
    items at page edges (e.g. 1.14). Clamping is safer than rejecting —
    a label that's partially off-page is still useful information.
    """
    return max(0.0, min(1.0, float(v)))


class BoundingBox(BaseModel):
    """Normalised page coordinates in [0, 1]. (0,0) is top-left."""

    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)

    @field_validator("x", "y", mode="before")
    @classmethod
    def _coerce_xy(cls, v: float) -> float:
        return _clamp01(v)

    @field_validator("width", "height", mode="before")
    @classmethod
    def _coerce_wh(cls, v: float) -> float:
        # Tolerate values up to 1.5 (small overflow from edge labels) and
        # clamp the upper bound to 1.0 — a width > 1 makes no geometric sense.
        return min(1.0, max(0.0, float(v)))


# -----------------------------------------------------------------------------
# Internal schemas — used to validate Claude's tool-call output
# -----------------------------------------------------------------------------


class SheetMetadata(BaseModel):
    sheet_number: str | None = None  # e.g. "S1.1"
    sheet_title: str | None = None
    discipline: (
        Literal[
            "civil",
            "site",
            "architectural",
            "interior",
            "structural",
            "mechanical",
            "plumbing",
            "electrical",
            "fire-protection",
            "general",
            "other",
        ]
        | None
    ) = None
    drawing_scale: str | None = None


class ScheduleIn(BaseModel):
    name: str
    columns: list[str]
    rows: list[dict]
    bbox: BoundingBox | None = None


class NoteIn(BaseModel):
    text: str
    bbox: BoundingBox | None = None


class CrossReferenceIn(BaseModel):
    target_sheet: str
    detail_id: str | None = None
    context: str | None = None
    bbox: BoundingBox | None = None


EntityType = Literal[
    "material",
    "manufacturer",
    "code",
    "dimension",
    "room",
    "equipment",
    "symbol",
    "other",
]


class EntityIn(BaseModel):
    entity_type: EntityType
    value: str
    bbox: BoundingBox | None = None
    extra: dict | None = None


class PageExtractionIn(BaseModel):
    """Schema Claude is constrained to return."""

    sheet_metadata: SheetMetadata = Field(default_factory=SheetMetadata)
    schedules: list[ScheduleIn] = Field(default_factory=list)
    notes: list[NoteIn] = Field(default_factory=list)
    cross_references: list[CrossReferenceIn] = Field(default_factory=list)
    entities: list[EntityIn] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# API output schemas
# -----------------------------------------------------------------------------


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    columns: list[str]
    rows: list[dict]
    bbox: dict | None


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    text: str
    bbox: dict | None


class CrossReferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    target_sheet: str
    detail_id: str | None
    context: str | None
    bbox: dict | None


class EntityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    entity_type: str
    value: str
    bbox: dict | None
    extra: dict | None


class PageExtractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    document_id: str
    page_id: str
    page_number: int

    sheet_number: str | None
    sheet_title: str | None
    discipline: str | None
    drawing_scale: str | None

    status: str
    error: str | None

    model: str | None
    cost_usd: float | None
    latency_ms: int | None

    created_at: datetime
    extracted_at: datetime | None

    schedules: list[ScheduleOut] = []
    notes: list[NoteOut] = []
    cross_references: list[CrossReferenceOut] = []
    entities: list[EntityOut] = []


class PageExtractionSummary(BaseModel):
    """Lightweight per-page summary used in document-level extraction overview."""

    model_config = ConfigDict(from_attributes=True)
    page_number: int
    status: str
    sheet_number: str | None
    sheet_title: str | None
    discipline: str | None
    schedule_count: int
    note_count: int
    cross_reference_count: int
    entity_count: int
    cost_usd: float | None


class DocumentExtractionOverview(BaseModel):
    document_id: str
    total_pages: int
    pages_ready: int
    pages_failed: int
    pages_pending: int
    pages_extracting: int
    total_cost_usd: float
    pages: list[PageExtractionSummary]
