"""Phase 4.2 endpoints — Trade Relevance Filter."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..models import Project, TradeDivisionRelevance
from ..schemas import (
    TradeDivisionRelevanceOut,
    TradeRelevanceMatrix,
    TradeRelevanceOverrideIn,
)
from ..services.trade_filter import (
    TradeFilterUnavailable,
    filter_trades,
    set_operator_override,
)
from .deps import DB, CurrentUser

router = APIRouter(
    prefix="/projects/{project_id}/trade-relevance", tags=["trade-relevance"]
)


def _to_matrix(project_id: str, rows: list[TradeDivisionRelevance]) -> TradeRelevanceMatrix:
    relevant = sum(1 for r in rows if r.effective_relevance)
    total_cost = sum(r.cost_usd or 0.0 for r in rows)
    return TradeRelevanceMatrix(
        project_id=project_id,
        relevant_count=relevant,
        skipped_count=len(rows) - relevant,
        total_cost_usd=total_cost,
        divisions=[TradeDivisionRelevanceOut.model_validate(r) for r in rows],
    )


@router.get("", response_model=TradeRelevanceMatrix)
async def get_relevance(
    project_id: str, db: DB, _: CurrentUser
) -> TradeRelevanceMatrix:
    proj = await db.execute(select(Project).where(Project.id == project_id))
    if proj.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    result = await db.execute(
        select(TradeDivisionRelevance)
        .where(TradeDivisionRelevance.project_id == project_id)
        .order_by(TradeDivisionRelevance.csi_division)
    )
    return _to_matrix(project_id, list(result.scalars().all()))


@router.post("", response_model=TradeRelevanceMatrix)
async def run_relevance_filter(
    project_id: str, db: DB, _: CurrentUser, force: bool = False
) -> TradeRelevanceMatrix:
    proj = await db.execute(select(Project).where(Project.id == project_id))
    if proj.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    try:
        rows = await filter_trades(project_id, force=force)
    except TradeFilterUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"trade filter failed: {e}",
        ) from e
    return _to_matrix(project_id, rows)


@router.patch("", response_model=TradeDivisionRelevanceOut)
async def override_relevance(
    project_id: str,
    payload: TradeRelevanceOverrideIn,
    db: DB,
    _: CurrentUser,
) -> TradeDivisionRelevanceOut:
    """Operator override for a single division. Phase 4.3 honours
    `effective_relevance` (override_value if set, else is_relevant)."""
    proj = await db.execute(select(Project).where(Project.id == project_id))
    if proj.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    try:
        row = await set_operator_override(
            project_id, payload.csi_division, payload.is_relevant
        )
    except TradeFilterUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    return TradeDivisionRelevanceOut.model_validate(row)
