"""Phase 4.3 / 4.4 endpoints — Scope of Work."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..models import Project, RagasEvalRun, ScopeExtractionRun, ScopeItem
from ..schemas import (
    RagasEvalRunOut,
    ScopeItemOut,
    ScopeOverview,
    ScopeRunOut,
    TrustScoreOut,
)
from ..services.ragas_runner import default_fixtures_path, schedule_ragas_eval
from ..services.scope_runner import (
    ScopeRunnerUnavailable,
    schedule_scope_run,
)
from ..services.sow_renderer import render_sow_for_run
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/scope", tags=["scope"])


async def _ensure_project(db, project_id: str) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    proj = result.scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return proj


@router.get("", response_model=ScopeOverview)
async def get_overview(
    project_id: str, db: DB, _: CurrentUser
) -> ScopeOverview:
    await _ensure_project(db, project_id)

    # Latest run
    run_result = await db.execute(
        select(ScopeExtractionRun)
        .where(ScopeExtractionRun.project_id == project_id)
        .order_by(ScopeExtractionRun.started_at.desc())
        .limit(1)
    )
    latest_run = run_result.scalar_one_or_none()

    if latest_run is None:
        return ScopeOverview(
            project_id=project_id,
            latest_run=None,
            total_items=0,
            by_division=[],
        )

    # Items from this run only
    items_result = await db.execute(
        select(ScopeItem.csi_division, ScopeItem.division_label).where(
            ScopeItem.run_id == latest_run.id
        )
    )
    rows = list(items_result.all())
    counter: Counter[tuple[str, str]] = Counter()
    for div_code, div_label in rows:
        counter[(div_code, div_label)] += 1
    by_division = sorted(
        [
            {"csi_division": k[0], "division_label": k[1], "count": v}
            for k, v in counter.items()
        ],
        key=lambda x: x["csi_division"],
    )
    return ScopeOverview(
        project_id=project_id,
        latest_run=ScopeRunOut.model_validate(latest_run),
        total_items=len(rows),
        by_division=by_division,
    )


@router.post("/runs", response_model=ScopeRunOut, status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    project_id: str, db: DB, _: CurrentUser
) -> ScopeRunOut:
    """Kick off a Phase 4.3 run in the background. Pre-conditions are checked
    synchronously so the user gets a clear 400 if the project isn't ready.
    """
    await _ensure_project(db, project_id)

    # Synchronous pre-condition probe so we 400 fast (without spawning a task)
    from ..services.project_profiler import ProfilerUnavailable

    from ..models import ProjectProfile, TradeDivisionRelevance
    from ..services.trade_list_parser import get_taxonomy_for_project

    taxonomy = await get_taxonomy_for_project(project_id)
    if taxonomy is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "no Trade_List.xlsx uploaded — upload one as a project document, "
                "wait for it to classify as 'trade-list', then retry"
            ),
        )

    prof = await db.execute(
        select(ProjectProfile).where(ProjectProfile.project_id == project_id)
    )
    if prof.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project not yet profiled — run Phase 4.1 (Generate profile) first",
        )

    rel = await db.execute(
        select(TradeDivisionRelevance).where(
            TradeDivisionRelevance.project_id == project_id
        )
    )
    if rel.scalars().first() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "trade relevance not yet computed — run Phase 4.2 "
                "(Filter relevant trades) first"
            ),
        )

    # Schedule background run
    schedule_scope_run(project_id)

    # Wait briefly for the runner to create its row, so we can return it
    import asyncio as _asyncio

    for _ in range(20):
        await _asyncio.sleep(0.1)
        latest = await db.execute(
            select(ScopeExtractionRun)
            .where(ScopeExtractionRun.project_id == project_id)
            .order_by(ScopeExtractionRun.started_at.desc())
            .limit(1)
        )
        run = latest.scalar_one_or_none()
        if run is not None and run.status == "running":
            return ScopeRunOut.model_validate(run)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="run was scheduled but never appeared in the database",
    )


@router.get("/runs/{run_id}", response_model=ScopeRunOut)
async def get_run(
    project_id: str, run_id: str, db: DB, _: CurrentUser
) -> ScopeRunOut:
    await _ensure_project(db, project_id)
    result = await db.execute(
        select(ScopeExtractionRun).where(
            ScopeExtractionRun.id == run_id,
            ScopeExtractionRun.project_id == project_id,
        )
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )
    return ScopeRunOut.model_validate(run)


@router.get("/runs/{run_id}/sow.md", response_class=PlainTextResponse)
async def get_sow_markdown(
    project_id: str, run_id: str, db: DB, _: CurrentUser
) -> Response:
    """Render the run as an industry-standard 10-section Scope of Work
    document in Markdown. Format follows Procore/Smartsheet/BuildBook
    conventions: project info, scope summary, included work organized
    by CSI division, exclusions, assumptions, materials/specs, schedule,
    submittals/closeout, coordination, change-order process.
    """
    await _ensure_project(db, project_id)
    result = await db.execute(
        select(ScopeExtractionRun).where(
            ScopeExtractionRun.id == run_id,
            ScopeExtractionRun.project_id == project_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )
    md = await render_sow_for_run(run_id)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="scope-of-work-{run_id[:8]}.md"'
            ),
        },
    )


@router.get("/items", response_model=list[ScopeItemOut])
async def list_items(
    project_id: str,
    db: DB,
    _: CurrentUser,
    csi_division: str | None = None,
) -> list[ScopeItemOut]:
    await _ensure_project(db, project_id)
    # Latest run only — older runs are kept for history but UI focuses on
    # the most recent.
    run_result = await db.execute(
        select(ScopeExtractionRun)
        .where(ScopeExtractionRun.project_id == project_id)
        .order_by(ScopeExtractionRun.started_at.desc())
        .limit(1)
    )
    latest = run_result.scalar_one_or_none()
    if latest is None:
        return []

    q = (
        select(ScopeItem)
        .options(selectinload(ScopeItem.citations))
        .where(ScopeItem.project_id == project_id)
        .where(ScopeItem.run_id == latest.id)
        .order_by(ScopeItem.csi_code)
    )
    if csi_division:
        q = q.where(ScopeItem.csi_division == csi_division)
    result = await db.execute(q)
    return [ScopeItemOut.model_validate(it) for it in result.scalars().all()]


@router.get("/items/{item_id}", response_model=ScopeItemOut)
async def get_item(
    project_id: str, item_id: str, db: DB, _: CurrentUser
) -> ScopeItemOut:
    await _ensure_project(db, project_id)
    result = await db.execute(
        select(ScopeItem)
        .options(selectinload(ScopeItem.citations))
        .where(ScopeItem.id == item_id, ScopeItem.project_id == project_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="scope item not found"
        )
    return ScopeItemOut.model_validate(item)


@router.post(
    "/runs/{run_id}/ragas",
    response_model=RagasEvalRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_ragas_eval(
    project_id: str, run_id: str, db: DB, _: CurrentUser
) -> RagasEvalRunOut:
    """Benchmark a completed scope run against curated ground-truth fixtures.

    Costs ~$1 / run, takes ~80s. Returns the eval row immediately
    (status='running'); UI polls GET /ragas until status='complete'.
    """
    await _ensure_project(db, project_id)
    scope_run = (
        await db.execute(
            select(ScopeExtractionRun).where(
                ScopeExtractionRun.id == run_id,
                ScopeExtractionRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if scope_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )
    if scope_run.status != "complete":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope run not complete (status={scope_run.status})",
        )

    fixtures_path = default_fixtures_path()
    eval_run = RagasEvalRun(
        project_id=project_id,
        scope_run_id=run_id,
        status="running",
        fixtures_path=fixtures_path,
    )
    db.add(eval_run)
    await db.commit()
    await db.refresh(eval_run)

    schedule_ragas_eval(eval_run.id, fixtures_path)
    return RagasEvalRunOut.model_validate(eval_run)


@router.get(
    "/runs/{run_id}/ragas",
    response_model=RagasEvalRunOut | None,
)
async def get_latest_ragas_eval(
    project_id: str, run_id: str, db: DB, _: CurrentUser
) -> RagasEvalRunOut | None:
    """Latest RAGAS eval for this scope run, or null if none yet."""
    await _ensure_project(db, project_id)
    row = (
        await db.execute(
            select(RagasEvalRun)
            .where(RagasEvalRun.scope_run_id == run_id)
            .where(RagasEvalRun.project_id == project_id)
            .order_by(RagasEvalRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return RagasEvalRunOut.model_validate(row)


@router.get("/trust-score", response_model=TrustScoreOut | None)
async def get_trust_score(
    project_id: str, db: DB, _: CurrentUser
) -> TrustScoreOut | None:
    """Trust score for the latest run. Returns null if no run exists yet."""
    await _ensure_project(db, project_id)
    run = (
        await db.execute(
            select(ScopeExtractionRun)
            .where(ScopeExtractionRun.project_id == project_id)
            .order_by(ScopeExtractionRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None or run.trust_score is None:
        return None
    payload = run.trust_score_components or {}
    return TrustScoreOut(
        score=run.trust_score,
        tier=payload.get("tier") or "RED",
        components=payload.get("components") or {},
        weights=payload.get("weights") or {},
        tier_thresholds=payload.get("tier_thresholds") or {},
        dropped_components=payload.get("dropped_components") or [],
        rationale=payload.get("rationale") or "",
    )
