"""Helper for writing AuditLog rows.

Use sparingly — AuditLog is for *decisions*, not for every API call (that's
LLMCall). Examples of what belongs here:

    - System auto-classified an item's evidence_tier as INFERRED_LOW_CONFIDENCE
    - Auto-arbitrator resolved a conflict (records winner + reasoning)
    - Operator manually resolved a conflict, acknowledged a gap, or moved an
      item between trade packages
    - Output generation produced a SOW or gap report

The activity feed on the project dashboard reads from this table.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog

log = logging.getLogger(__name__)


async def record_audit(
    db: AsyncSession,
    *,
    project_id: str,
    action: str,
    entity_type: str,
    actor: str,
    entity_id: str | None = None,
    run_id: str | None = None,
    payload: dict[str, Any] | None = None,
    note: str | None = None,
    flush: bool = True,
) -> AuditLog:
    """Persist an AuditLog row.

    Mirrors the shape of ``llm_log.record_call`` so callers feel consistent.
    Caller is responsible for the surrounding session/commit lifecycle.

    Parameters
    ----------
    db
        Active async session.
    project_id, run_id
        Scope. ``run_id`` is nullable for project-level actions
        (e.g., overriding trade relevance outside any specific run).
    action
        Verb: "create" | "resolve" | "acknowledge" | "override" |
        "reclassify" | "merge" | "reject" | "export" | "regenerate" | etc.
    entity_type
        What the row is about: "scope_item" | "conflict" | "gap" |
        "trade_package" | "output" | "trade_relevance" | etc.
    entity_id
        UUID of the affected entity (nullable for project-wide actions).
    actor
        "system:<service-name>" for automated decisions, "user:<email>" for
        operator actions.
    payload
        Free-shape JSON. Convention for state changes:
        ``{"before": ..., "after": ...}``.
    note
        Optional human-readable note (e.g., operator's resolution reasoning).
    flush
        Whether to flush the session immediately (default True). Set False
        when batching multiple records inside a single transaction.
    """
    row = AuditLog(
        project_id=project_id,
        run_id=run_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        payload=payload,
        note=note,
    )
    db.add(row)
    if flush:
        await db.flush()
    return row
