"""Models for Phase 4.1 (Project Profile) and Phase 4.2 (Trade Relevance).

These are project-level metadata derived from the indexed corpus. They feed
the Phase 4.3 scope extractor, which uses them to scope queries (project
profile) and skip irrelevant CSI divisions (relevance filter).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectProfile(Base):
    """One per project. Building type, size, codes, etc."""

    __tablename__ = "project_profiles"
    __table_args__ = (UniqueConstraint("project_id", name="uq_project_profile"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    building_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_sf: Mapped[float | None] = mapped_column(Float, nullable=True)
    occupancy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    construction_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sprinklered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stories: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    codes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audit
    raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TradeDivisionRelevance(Base):
    """One per (project, csi_division). 'Should Phase 4.3 process this division?'"""

    __tablename__ = "trade_division_relevance"
    __table_args__ = (
        UniqueConstraint("project_id", "csi_division", name="uq_proj_div"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    csi_division: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    division_label: Mapped[str] = mapped_column(String(255), nullable=False)

    is_relevant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Operator override — if true, ignore is_relevant and use override_value
    operator_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    override_value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def effective_relevance(self) -> bool:
        if self.operator_override and self.override_value is not None:
            return self.override_value
        return self.is_relevant
