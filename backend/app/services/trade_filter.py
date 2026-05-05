"""Phase 4.2 — Trade Relevance Filter.

For each CSI division in the project's uploaded Trade_List.xlsx, ask Claude
Haiku 4.5: 'given this project profile, is this trade applicable?'

Cheap (one Haiku call per division × ~34 divisions ≈ $0.10), parallel, and
gates Phase 4.3 — we only run scope extraction over relevant divisions, so
we never waste expensive Sonnet calls on Marine / Transportation / etc.
for a small commercial project.

Operator override is supported: if the Haiku verdict is wrong, the operator
can flip a chip in the UI and Phase 4.3 honours `effective_relevance`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ProjectProfile, TradeDivisionRelevance
from .llm_log import Usage, record_call, usage_from_anthropic
from .trade_list_parser import CSIDivision, CSITaxonomy

log = logging.getLogger(__name__)


class TradeFilterUnavailable(Exception):
    """Raised when prerequisites are missing (no API key, no trade list, no profile)."""


_RELEVANCE_TOOL = {
    "name": "judge_trade_relevance",
    "description": (
        "Decide if a CSI MasterFormat division applies to a given construction "
        "project. Be inclusive: when in doubt, mark relevant. Only mark "
        "not-relevant if the trade is clearly inapplicable (e.g. Division 35 "
        "Marine on a small commercial building, Division 40 Process "
        "Interconnections on a community center)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_relevant": {"type": "boolean"},
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0.0 = unsure, 1.0 = certain.",
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence justifying the decision.",
            },
        },
        "required": ["is_relevant", "confidence", "reasoning"],
    },
}


def _get_client():
    if not settings.anthropic_api_key:
        raise TradeFilterUnavailable("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _format_profile_for_prompt(profile: ProjectProfile) -> str:
    lines = ["Project profile:"]
    if profile.building_type:
        lines.append(f"  - Building type: {profile.building_type}")
    if profile.size_sf:
        lines.append(f"  - Size: {profile.size_sf:,.0f} SF")
    if profile.occupancy:
        lines.append(f"  - Occupancy: {profile.occupancy}")
    if profile.construction_type:
        lines.append(f"  - Construction: {profile.construction_type}")
    if profile.sprinklered is not None:
        lines.append(f"  - Sprinklered: {profile.sprinklered}")
    if profile.stories is not None:
        lines.append(f"  - Stories: {profile.stories}")
    if profile.location:
        lines.append(f"  - Location: {profile.location}")
    if profile.codes:
        lines.append(f"  - Codes: {', '.join(profile.codes)}")
    return "\n".join(lines)


def _build_division_prompt(profile: ProjectProfile, division: CSIDivision) -> str:
    profile_str = _format_profile_for_prompt(profile)
    sections_summary = "\n".join(
        f"  {s.code} — {s.title}" for s in division.sections[:25]
    )
    if len(division.sections) > 25:
        sections_summary += f"\n  ... and {len(division.sections) - 25} more"
    return (
        f"{profile_str}\n\n"
        f"CSI Division: {division.label}\n"
        f"Sections in this division (first 25):\n{sections_summary}\n\n"
        "Use the judge_trade_relevance tool now."
    )


async def _judge_one_division(
    client,
    project_id: str,
    profile: ProjectProfile,
    division: CSIDivision,
    sem: asyncio.Semaphore,
) -> tuple[CSIDivision, dict, Usage, int]:
    async with sem:
        t0 = time.perf_counter()
        msg = await client.messages.create(
            model=settings.classifier_model,  # Haiku 4.5
            max_tokens=512,
            tools=[_RELEVANCE_TOOL],
            tool_choice={"type": "tool", "name": "judge_trade_relevance"},
            messages=[
                {
                    "role": "user",
                    "content": _build_division_prompt(profile, division),
                }
            ],
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
    verdict: dict | None = None
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "judge_trade_relevance"
        ):
            verdict = block.input
            break
    if verdict is None:
        raise RuntimeError(
            f"trade_filter: no tool_use for division {division.code}"
        )
    return division, verdict, usage_from_anthropic(msg), latency_ms


async def filter_trades(project_id: str, *, force: bool = False) -> list[TradeDivisionRelevance]:
    """Run the relevance filter for every division in the project's trade list.

    `force=True` re-runs even if rows already exist. Otherwise skips divisions
    that already have a row (operator may have manually overridden some).
    """
    from ..database import SessionLocal
    from .trade_list_parser import get_taxonomy_for_project

    taxonomy = await get_taxonomy_for_project(project_id)
    if taxonomy is None:
        raise TradeFilterUnavailable(
            "no Trade_List.xlsx uploaded — upload one as a project document first"
        )

    async with SessionLocal() as db:
        prof_result = await db.execute(
            select(ProjectProfile).where(ProjectProfile.project_id == project_id)
        )
        profile = prof_result.scalar_one_or_none()
        if profile is None:
            raise TradeFilterUnavailable(
                "project not yet profiled — run Phase 4.1 first"
            )

        existing = await db.execute(
            select(TradeDivisionRelevance).where(
                TradeDivisionRelevance.project_id == project_id
            )
        )
        existing_codes = {r.csi_division for r in existing.scalars().all()}

    divisions_to_check = [
        d for d in taxonomy.divisions if force or d.code not in existing_codes
    ]
    if not divisions_to_check:
        log.info("trade_filter: all %d divisions already filtered", len(taxonomy.divisions))
        async with SessionLocal() as db:
            r = await db.execute(
                select(TradeDivisionRelevance)
                .where(TradeDivisionRelevance.project_id == project_id)
                .order_by(TradeDivisionRelevance.csi_division)
            )
            return list(r.scalars().all())

    log.info(
        "trade_filter: judging %d divisions for project %s",
        len(divisions_to_check),
        project_id,
    )
    client = _get_client()
    sem = asyncio.Semaphore(10)
    results = await asyncio.gather(
        *(
            _judge_one_division(client, project_id, profile, d, sem)
            for d in divisions_to_check
        ),
        return_exceptions=True,
    )

    rows: list[TradeDivisionRelevance] = []
    async with SessionLocal() as db:
        for r in results:
            if isinstance(r, Exception):
                log.warning("trade_filter: division failed: %s", r)
                continue
            division, verdict, usage, latency_ms = r
            cost, _ = await record_call(
                db,
                purpose="trade-relevance",
                model=settings.classifier_model,
                usage=usage,
                latency_ms=latency_ms,
                project_id=project_id,
            )
            row = TradeDivisionRelevance(
                project_id=project_id,
                csi_division=division.code,
                division_label=division.label,
                is_relevant=bool(verdict["is_relevant"]),
                reasoning=verdict.get("reasoning"),
                confidence=float(verdict.get("confidence", 0.0)),
                operator_override=False,
                override_value=None,
                cost_usd=cost,
                latency_ms=latency_ms,
            )
            db.add(row)
            rows.append(row)
        await db.commit()

    # Re-fetch all rows (including any pre-existing ones not in this batch)
    async with SessionLocal() as db:
        r = await db.execute(
            select(TradeDivisionRelevance)
            .where(TradeDivisionRelevance.project_id == project_id)
            .order_by(TradeDivisionRelevance.csi_division)
        )
        all_rows = list(r.scalars().all())

    log.info(
        "trade_filter: project %s — %d relevant, %d skipped, total cost $%.4f",
        project_id,
        sum(1 for x in all_rows if x.effective_relevance),
        sum(1 for x in all_rows if not x.effective_relevance),
        sum(x.cost_usd or 0.0 for x in all_rows),
    )
    return all_rows


async def set_operator_override(
    project_id: str, csi_division: str, is_relevant: bool
) -> TradeDivisionRelevance:
    """Operator-side flip: force a division relevant or not."""
    from ..database import SessionLocal

    async with SessionLocal() as db:
        result = await db.execute(
            select(TradeDivisionRelevance).where(
                TradeDivisionRelevance.project_id == project_id,
                TradeDivisionRelevance.csi_division == csi_division,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise TradeFilterUnavailable(
                f"no relevance row for division {csi_division}"
            )
        row.operator_override = True
        row.override_value = is_relevant
        await db.commit()
        await db.refresh(row)
        return row
