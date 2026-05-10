"""Post-vision enrichment passes for drawing-set documents.

After a drawing PDF has been rendered + vision-extracted, four
independent passes enrich the database with derived metadata:

    sheet_index    — cover-sheet drawing index → SheetIndex rows
    schedule       — discover schedule grids → typed schedule rows
    revision       — revision-block stamps → RevisionBlock rows
    symbol_legend  — Symbols & Abbreviations legends → SymbolLegend rows

Until this module landed, each pass was hand-wired in `processor.py`
as a sequential `await` with its own try/except + log.info / log.exception
boilerplate. Wall time was the *sum* of all four (~6 minutes on the
Elks drawing set) because none of them depends on the others' output —
they were just written one after another.

Each pass exposes the same shape (`async run(ctx) → EnrichmentResult`),
so the orchestrator can declare the four as a list and `asyncio.gather`
them. Wall time becomes the *max* of the four (~2.8 min on the same
drawing set) — about half.

Adding a fifth pass is a one-line registry entry; the orchestrator
stays unchanged.

Soft dependencies: `symbol_legend`'s candidate identification reads
SheetIndex (path A) AND PageExtraction (path B). Path A is empty if
sheet_index hasn't run yet, but path B works independently and finds
the same legend sheets in practice. Net effect of running them in
parallel: identical legend output, half the wall time.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class EnrichmentContext:
    """Inputs every enrichment pass needs."""

    document_id: str
    project_id: str
    filename: str  # for logging only


@dataclass
class EnrichmentResult:
    """One pass's outcome — for the orchestrator's log line + cost rollup."""

    summary: str
    cost_usd: float = 0.0
    failed: bool = False


class EnrichmentPass(ABC):
    """One post-vision enrichment pass.

    The interface IS the test surface: subclasses are deep (one job each),
    the abstraction is shallow on purpose so the orchestrator can treat
    them uniformly.
    """

    name: str = "unnamed"

    @abstractmethod
    async def run(self, ctx: EnrichmentContext) -> EnrichmentResult:
        """Run the pass. Should be idempotent — orchestrator may invoke
        on a document whose previous run was interrupted."""


# ============================================================================
# Concrete passes — each wraps the existing service module
# ============================================================================


class SheetIndexPass(EnrichmentPass):
    """Cover-sheet drawing index → SheetIndex rows."""

    name = "sheet_index"

    async def run(self, ctx: EnrichmentContext) -> EnrichmentResult:
        from . import sheet_index_extractor

        n_sheets, cost = await sheet_index_extractor.extract_for_document(
            ctx.document_id
        )
        return EnrichmentResult(
            summary=f"{n_sheets} sheets",
            cost_usd=cost,
        )


class SchedulePass(EnrichmentPass):
    """Discover schedule grids → typed schedule rows.

    Two-step internally (router → typed extractor) because the typed
    extractor consumes the router's output. Both run inside this single
    pass so the orchestrator sees one unit.
    """

    name = "schedule"

    async def run(self, ctx: EnrichmentContext) -> EnrichmentResult:
        from . import schedule_extractor_typed, schedule_router

        rr = await schedule_router.route_for_document(ctx.document_id)
        if not rr.schedules:
            return EnrichmentResult(
                summary=(
                    f"{rr.schedules_found} schedules in {rr.pages_classified} "
                    f"pages, none extractable"
                ),
                cost_usd=rr.cost_usd,
            )
        typed = await schedule_extractor_typed.extract_for_routed_schedules(
            rr.schedules
        )
        return EnrichmentResult(
            summary=(
                f"{rr.schedules_found} schedules / {typed['extracted']}/"
                f"{typed['requests']} typed / {typed['rows']} rows"
            ),
            cost_usd=rr.cost_usd + (typed.get("cost_usd") or 0.0),
        )


class RevisionPass(EnrichmentPass):
    """Revision-block stamps on each sheet → RevisionBlock rows."""

    name = "revision"

    async def run(self, ctx: EnrichmentContext) -> EnrichmentResult:
        from . import revision_block_parser

        rev = await revision_block_parser.parse_for_document(ctx.document_id)
        return EnrichmentResult(
            summary=f"{rev['revisions']} revisions in {rev['pages']} pages",
            cost_usd=rev.get("cost_usd") or 0.0,
        )


class SymbolLegendPass(EnrichmentPass):
    """Symbols & Abbreviations sheets → SymbolLegend rows.

    Project-scoped (not document-scoped) because legends usually live on
    one or two G-series sheets that cover the whole drawing set, and the
    extractor is idempotent (wipes + re-extracts) so re-running per
    drawing-set upload is safe.
    """

    name = "symbol_legend"

    async def run(self, ctx: EnrichmentContext) -> EnrichmentResult:
        from .symbol_legend_extractor import extract_for_project

        leg = await extract_for_project(ctx.project_id)
        return EnrichmentResult(
            summary=(
                f"{leg['entries']} entries from "
                f"{leg['sheets_processed']} sheets"
            ),
            cost_usd=leg.get("cost_usd") or 0.0,
        )


# ============================================================================
# Registry + orchestrator
# ============================================================================


# Order is for log readability only; passes run concurrently via gather.
_DRAWING_PASSES: list[type[EnrichmentPass]] = [
    SheetIndexPass,
    SchedulePass,
    RevisionPass,
    SymbolLegendPass,
]


async def run_drawing_enrichment(ctx: EnrichmentContext) -> dict[str, EnrichmentResult]:
    """Run every drawing-set enrichment pass in parallel.

    Each pass is fault-isolated: an exception in one is logged and
    recorded as a failed `EnrichmentResult`, and the others still run
    to completion. This matches the previous sequential behavior where
    each pass had its own try/except.

    Returns a `{name: result}` map so the caller can reason about
    per-pass outcomes (e.g. roll up cost, decide whether to retry).
    """

    async def _run_one(pass_cls: type[EnrichmentPass]) -> tuple[str, EnrichmentResult]:
        instance = pass_cls()
        try:
            result = await instance.run(ctx)
            log.info(
                "enrichment: %s — %s ($%.4f) for %s",
                instance.name, result.summary, result.cost_usd, ctx.filename,
            )
            return instance.name, result
        except Exception as e:  # noqa: BLE001 — fault-isolate per pass
            log.exception("enrichment: %s failed for %s: %s", instance.name, ctx.filename, e)
            return instance.name, EnrichmentResult(
                summary=f"FAILED: {type(e).__name__}: {e}",
                cost_usd=0.0,
                failed=True,
            )

    pairs = await asyncio.gather(*(_run_one(p) for p in _DRAWING_PASSES))
    return dict(pairs)
