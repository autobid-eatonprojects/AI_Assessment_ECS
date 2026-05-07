"""Phase 4.2 — Trade Relevance Filter.

For each CSI division in the project's uploaded Trade_List.xlsx, ask Claude
Haiku 4.5: 'given this project profile, is this trade applicable?'

Cheap (one Haiku call per division × ~34 divisions ≈ $0.10), parallel, and
gates Phase 4.3 — we only run scope extraction over relevant divisions, so
we never waste expensive Sonnet calls on Marine / Transportation / etc.
for a small commercial project.

Operator override is supported: if the Haiku verdict is wrong, the operator
can flip a chip in the UI and Phase 4.3 honours `effective_relevance`.

Corpus-evidence gate
--------------------
The LLM is told to "be inclusive: when in doubt, mark relevant" — that produces
optimistic verdicts on building-type stereotypes (e.g. "community centers
*usually* have kitchen equipment, so Div 11 is relevant") even when the actual
spec/drawings don't contain that division. To kill those false positives we
post-filter every True verdict against actual corpus evidence:

    final_is_relevant = llm_says_true AND corpus_has_evidence

Corpus evidence is detected from two sources:
  1. Spec book — looks for `DIVISION NN` or `SECTION NN XX XX` headers.
     The "SECTION" / "DIVISION" prefix is what filters AIA contract-clause
     index ghosts (e.g. "13.4.4.1" → "13 40 41" after OCR strips periods).
  2. Drawings — sheet IDs map to disciplines via SHEET_PREFIX_DISCIPLINE,
     and disciplines own divisions (per discipline_mapping.yaml). An `M`
     sheet implies Div 23 evidence; a `P` sheet implies Div 22; etc.
This catches MEP trades whose specs live in drawings + cut sheets rather
than in formal spec sections.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    Chunk,
    Document,
    ProjectProfile,
    SheetIndex,
    TradeDivisionRelevance,
)
from .discipline_config import discipline_for_sheet_prefix, load_disciplines
from .llm_log import Usage, record_call, usage_from_anthropic
from .trade_list_parser import CSIDivision, CSITaxonomy

log = logging.getLogger(__name__)


class TradeFilterUnavailable(Exception):
    """Raised when prerequisites are missing (no API key, no trade list, no profile)."""


def _division_in_spec_text(div_code: str, spec_text: str) -> bool:
    """True if `div_code` appears as a real spec header in `spec_text`.

    Real headers come in two forms — `DIVISION NN` or `SECTION NN XX XX`.
    The leading keyword is what distinguishes them from AIA contract-clause
    index ghosts where OCR loses the periods (e.g. "13.4.4.1" → "13 40 41").
    The spec author writes "DIVISION 1" (unpadded) for two-digit-displayed
    divisions, so we also accept the unpadded form.
    """
    if not spec_text:
        return False
    div_int = int(div_code)
    div_re = re.compile(rf"\bDIVISION\s+0*{div_int}\b", re.IGNORECASE)
    sec_re = re.compile(rf"\bSECTION\s+{div_code}\s\d{{2}}\s\d{{2}}\b", re.IGNORECASE)
    return bool(div_re.search(spec_text) or sec_re.search(spec_text))


def _divisions_with_drawing_evidence(sheet_ids: list[str]) -> set[str]:
    """For every sheet ID, map its prefix → discipline → owned divisions."""
    seen: set[str] = set()
    divs_by_discipline = {d.key: set(d.csi_divisions) for d in load_disciplines()}
    # Div 00/01 (general/admin) are always implicitly evidenced by any project
    # — no "DIVISION 0/1" header on a drawing, but every project contains them.
    seen |= divs_by_discipline.get("general", set())
    for sid in sheet_ids:
        disc = discipline_for_sheet_prefix(sid or "")
        if disc:
            # discipline_for_sheet_prefix returns the YAML 'key' for most
            # sheet prefixes, but 'L' returns 'landscape' which isn't a
            # discipline key — it falls back to 'site' downstream.
            owned = divs_by_discipline.get(disc, divs_by_discipline.get("site", set()))
            seen |= owned
    return seen


async def _gather_corpus_evidence(project_id: str) -> set[str]:
    """Return the set of CSI divisions that have actual project-corpus evidence.

    Combines spec-book header matches and drawing-sheet discipline mapping.
    Empty if neither spec nor drawings are available — caller should treat
    "no corpus" as "fall back to LLM verdict alone" (see filter_trades).
    """
    from ..database import SessionLocal

    async with SessionLocal() as db:
        # Spec text: concatenate all chunk text from any written-spec document.
        spec_text = ""
        spec_docs = (
            await db.execute(
                select(Document.id).where(
                    Document.project_id == project_id,
                    Document.doc_type == "written-spec",
                )
            )
        ).scalars().all()
        if spec_docs:
            chunk_texts = (
                await db.execute(
                    select(Chunk.text).where(Chunk.document_id.in_(spec_docs))
                )
            ).scalars().all()
            spec_text = "\n".join(t for t in chunk_texts if t)

        sheet_rows = (
            await db.execute(
                select(SheetIndex.sheet_id).where(SheetIndex.project_id == project_id)
            )
        ).scalars().all()

    if not spec_text and not sheet_rows:
        return set()

    # Divisions evidenced by spec headers
    evidenced: set[str] = set()
    if spec_text:
        # Quick scan: walk every two-digit group preceded by SECTION/DIVISION
        # once, instead of re-running a regex per division.
        for m in re.finditer(
            r"\b(?:DIVISION\s+0*(\d{1,2})|SECTION\s+(\d{2})\s\d{2}\s\d{2})\b",
            spec_text,
            re.IGNORECASE,
        ):
            div = (m.group(1) or m.group(2) or "").zfill(2)
            if div:
                evidenced.add(div)

    # Divisions evidenced by drawing sheets
    evidenced |= _divisions_with_drawing_evidence(list(sheet_rows))
    return evidenced


_RELEVANCE_TOOL = {
    "name": "judge_trade_relevance",
    "description": (
        "Decide if a CSI MasterFormat division applies to a given construction "
        "project. Be inclusive: when in doubt, mark relevant. Only mark "
        "not-relevant if the trade is clearly inapplicable (e.g. Division 35 "
        "Marine on a warehouse, Division 41 Material Processing on a "
        "small office tenant fit-out)."
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

    # Corpus-evidence gate: drop True verdicts that have no spec/drawing
    # support. Empty set means no corpus is available yet (no spec uploaded,
    # no sheets indexed) — in that case we fall back to the LLM verdict
    # alone so we don't block extraction on a chicken-and-egg problem.
    corpus_evidence = await _gather_corpus_evidence(project_id)
    apply_corpus_gate = bool(corpus_evidence)

    rows: list[TradeDivisionRelevance] = []
    flipped_by_corpus: list[str] = []
    async with SessionLocal() as db:
        if force:
            # Wipe existing rows so we can re-insert with the new verdicts.
            # Operator overrides live on the same row, so a force re-run
            # intentionally drops them — that matches the UI semantics
            # ("Re-generate" = start fresh).
            await db.execute(
                delete(TradeDivisionRelevance).where(
                    TradeDivisionRelevance.project_id == project_id
                )
            )
            await db.flush()
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
            llm_relevant = bool(verdict["is_relevant"])
            llm_reasoning = verdict.get("reasoning") or ""
            final_relevant = llm_relevant
            final_reasoning = llm_reasoning
            if (
                apply_corpus_gate
                and llm_relevant
                and division.code not in corpus_evidence
            ):
                final_relevant = False
                final_reasoning = (
                    f"{llm_reasoning} [Vetoed by corpus-evidence gate: no "
                    f"`DIVISION {int(division.code)}` or `SECTION "
                    f"{division.code} XX XX` header in spec, and no drawing "
                    f"sheet maps to this division.]"
                ).strip()
                flipped_by_corpus.append(division.code)
            row = TradeDivisionRelevance(
                project_id=project_id,
                csi_division=division.code,
                division_label=division.label,
                is_relevant=final_relevant,
                reasoning=final_reasoning,
                confidence=float(verdict.get("confidence", 0.0)),
                operator_override=False,
                override_value=None,
                cost_usd=cost,
                latency_ms=latency_ms,
            )
            db.add(row)
            rows.append(row)
        await db.commit()

    if flipped_by_corpus:
        log.info(
            "trade_filter: corpus-evidence gate vetoed %d LLM-True verdicts: %s",
            len(flipped_by_corpus),
            ",".join(sorted(flipped_by_corpus)),
        )

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


async def reevaluate_corpus_evidence(
    project_id: str, *, clear_stale_true_overrides: bool = False
) -> dict:
    """Re-run the corpus-evidence gate against existing relevance rows.

    Useful after the spec/drawings have been (re-)processed, or to clean up
    rows from a project that ran before this gate existed. Modifies
    `is_relevant` in place.

    When `clear_stale_true_overrides=True` we also clear `operator_override`
    on rows that match the unambiguous "stale True override" pattern: LLM
    voted False AND corpus has no evidence AND override_value forced True.
    These can only be artifacts of an earlier experimental session — the
    division can't apply (LLM and corpus agree) — and the override is
    actively producing noise in gap detection. We never touch overrides
    that contradict any single signal alone.
    """
    from ..database import SessionLocal

    corpus_evidence = await _gather_corpus_evidence(project_id)
    if not corpus_evidence:
        return {"flipped": [], "reason": "no corpus available — gate not applied"}

    flipped: list[str] = []
    cleared_overrides: list[str] = []
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(TradeDivisionRelevance).where(
                    TradeDivisionRelevance.project_id == project_id
                )
            )
        ).scalars().all()
        for row in rows:
            has_evidence = row.csi_division in corpus_evidence

            # Pass 1: corpus-evidence gate on is_relevant
            if row.is_relevant and not has_evidence:
                row.is_relevant = False
                note = (
                    f" [Vetoed by corpus-evidence gate: no `DIVISION "
                    f"{int(row.csi_division)}` or `SECTION {row.csi_division} "
                    f"XX XX` header in spec, and no drawing sheet maps to "
                    f"this division.]"
                )
                if row.reasoning and "[Vetoed by corpus-evidence gate" not in row.reasoning:
                    row.reasoning = row.reasoning + note
                elif not row.reasoning:
                    row.reasoning = note.strip()
                flipped.append(row.csi_division)

            # Pass 2: stale True-override cleanup
            if (
                clear_stale_true_overrides
                and row.operator_override
                and row.override_value is True
                and not row.is_relevant
                and not has_evidence
            ):
                row.operator_override = False
                row.override_value = None
                cleared_overrides.append(row.csi_division)
        await db.commit()

    log.info(
        "trade_filter.reevaluate: project %s — flipped %d, cleared %d stale overrides",
        project_id,
        len(flipped),
        len(cleared_overrides),
    )
    return {
        "flipped": sorted(flipped),
        "cleared_overrides": sorted(cleared_overrides),
        "evidenced_divisions": sorted(corpus_evidence),
    }
