import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Project lifecycle states. The state determines which actions are permitted
# (which upload zone is active, when Phase 4 / Phase 8 can run).
LIFECYCLE_STATES = ("setup", "open-for-bids", "complete")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle:
    #   setup           — GC uploading project documents (drawings/specs/trade list)
    #   open-for-bids   — scope locked, accepting bid submissions from subcontractors
    #   complete        — analysis done, awarded
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="setup"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    documents: Mapped[list["Document"]] = relationship(  # noqa: F821
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
