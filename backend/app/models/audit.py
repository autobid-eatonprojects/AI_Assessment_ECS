"""Phase 8 — Audit log for operator-visible decisions.

Distinct from `LLMCall` (which records every API call): AuditLog records
*decisions* — resolve-conflict, acknowledge-gap, override-relevance,
generate-output, etc. Used to (a) power the activity feed on the project
dashboard, (b) feed a future customer-feedback flywheel, (c) provide a
durable trail for "who changed what when" queries.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # Many actions are run-scoped (e.g. resolve-conflict, generate-output);
    # some are project-scoped (e.g. override-relevance). Nullable.
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # What the row is about: "scope_item" | "conflict" | "gap" | "trade_package"
    # | "output" | "trade_relevance" | etc. Freetext on purpose; new entity
    # types appear without a migration.
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # What happened: "create" | "resolve" | "acknowledge" | "override" |
    # "reclassify" | "merge" | "reject" | "export" | "regenerate" | etc.
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Who did it: "system:scope_runner" | "system:scope_verifier" |
    # "user:<email>". Use the dotted form so we can filter by source.
    actor: Mapped[str] = mapped_column(String(128), nullable=False)

    # Free-shape payload for the change: typically {"before": ..., "after": ...}
    # but action-specific. Reviewer UI renders this verbatim.
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Optional human-readable note (operator-supplied, e.g. resolution reasoning)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
