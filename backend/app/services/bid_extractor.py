"""Phase 8a — Bid line-item extractor.

For each `bid-quote` or `scope-letter` document, run a single Sonnet 4.6 call
with a structured tool that returns:

  - vendor_name (sometimes states a different vendor than what the operator
    typed at upload time — we keep both)
  - bid_total_usd (often present as a grand total at the bottom)
  - line_items[]: one per priced row (description, qty, unit, prices)
  - explicit_inclusions[] (clarifying what IS in scope)
  - explicit_exclusions[] (the load-bearing field for Phase 8 — bids that
    explicitly exclude an item are flagged red on the coverage matrix)
  - primary_csi_divisions[]: the model's best guess at which divisions
    this bid is competing for (used by the coverage matrix to skip
    unrelated scope items)

Bids in this dataset run from 1-page quotes to 50+ page proposals. We
concatenate every chunk's text for the bid and send as one call. If a
bid exceeds ~60k chars we truncate (Sonnet's 200k context fits this
easily, but cost grows linearly so we cap).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    BidExclusion,
    BidExtractionRun,
    BidInclusion,
    BidLineItem,
    BidSummary,
    Chunk,
    Document,
)
from .llm_log import Usage, record_call, usage_from_anthropic

log = logging.getLogger(__name__)


# Bids that are accepted into the extraction pipeline. Other bid-doc types
# (license-insurance, safety-manual, contractor-info, other) are supporting
# documents — we don't try to read line items out of them.
_PRICED_BID_TYPES = {"bid-quote", "scope-letter"}

# Soft cap on prompt size. Anything larger gets truncated to keep cost
# bounded. ~60k chars ≈ 15k tokens — fine for Sonnet.
_MAX_BID_PROMPT_CHARS = 60_000

# Concurrent bid extractions per project (5 bids × Sonnet ≈ rate-limit floor)
_EXTRACT_CONCURRENCY = 5


_EXTRACT_TOOL = {
    "name": "extract_bid",
    "description": (
        "Read a vendor bid (proposal, quote, or scope letter) and extract "
        "every priced line item, the bid total, and any explicit "
        "inclusions or exclusions. Be exhaustive — bids commonly have a "
        "main pricing block plus separate paragraphs of inclusions / "
        "exclusions / qualifications. Don't invent items."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor_name": {
                "type": "string",
                "description": (
                    "The bidding company name as printed on the bid. Null if "
                    "not stated."
                ),
            },
            "bid_total_usd": {
                "type": "number",
                "description": (
                    "Total bid price in USD if a single grand total is "
                    "stated; null if the bid has only line-item prices or no "
                    "explicit total."
                ),
            },
            "primary_csi_divisions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Best-guess CSI divisions (2-digit) this bid is competing "
                    "for. e.g. ['03'] for a concrete bid, ['09', '12'] for a "
                    "finishes+furnishings bid. Empty list if unclear."
                ),
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": (
                                "What the line covers — concise but specific "
                                "enough to compare against a scope item."
                            ),
                        },
                        "quantity": {
                            "type": "string",
                            "description": "Quantity if stated. Null if not.",
                        },
                        "unit": {
                            "type": "string",
                            "description": "Unit code (EA, LF, SF, CY…). Null if not.",
                        },
                        "unit_price_usd": {
                            "type": "number",
                            "description": "Unit price in USD. Null if not.",
                        },
                        "total_price_usd": {
                            "type": "number",
                            "description": "Line total in USD. Null if not.",
                        },
                        "csi_section_guess": {
                            "type": "string",
                            "description": (
                                "Best-guess CSI section as 'NN NN NN' or "
                                "'NN' if uncertain. Null if unclear."
                            ),
                        },
                        "page_number": {
                            "type": "integer",
                            "description": "Source page number if known.",
                        },
                    },
                    "required": ["description"],
                },
            },
            "explicit_inclusions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "page_number": {"type": "integer"},
                    },
                    "required": ["text"],
                },
                "description": (
                    "Clarifying inclusions stated in the bid (e.g. 'Includes "
                    "all labor and materials for slab on grade')."
                ),
            },
            "explicit_exclusions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "page_number": {"type": "integer"},
                    },
                    "required": ["text"],
                },
                "description": (
                    "Explicit exclusions stated in the bid (e.g. 'Excludes "
                    "rubber base, sinks, plumbing connections'). One per "
                    "entry — split lists into individual items."
                ),
            },
        },
        "required": ["line_items", "explicit_exclusions", "explicit_inclusions"],
    },
}


@dataclass
class BidExtraction:
    """Tool-output payload from extract_bid, plus call accounting."""

    vendor_name: str | None
    bid_total_usd: float | None
    primary_csi_divisions: list[str]
    line_items: list[dict]
    inclusions: list[dict]
    exclusions: list[dict]
    usage: Usage
    latency_ms: int


def _get_client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _gather_bid_text(db: AsyncSession, document_id: str) -> tuple[str, list[Chunk]]:
    """Pull every chunk for a bid document, ordered by page, return concat text + chunk list.

    We use ALL chunks (not Phase-3 retrieval) because we want the full bid
    fed to the model — bids are short, retrieval would just drop content.
    """
    chunks = (
        await db.execute(
            select(Chunk)
            .where(Chunk.document_id == document_id)
            .order_by(Chunk.page_number, Chunk.id)
        )
    ).scalars().all()

    parts: list[str] = []
    total_len = 0
    for c in chunks:
        page_marker = f"\n\n--- page {c.page_number or '?'} ---\n" if c.page_number else "\n\n"
        body = c.text or ""
        seg = page_marker + body
        if total_len + len(seg) > _MAX_BID_PROMPT_CHARS:
            parts.append(page_marker)
            parts.append(body[: max(0, _MAX_BID_PROMPT_CHARS - total_len - len(page_marker))])
            parts.append("\n\n[truncated — bid too long for single extraction call]")
            total_len = _MAX_BID_PROMPT_CHARS
            break
        parts.append(seg)
        total_len += len(seg)
    return "".join(parts).strip(), list(chunks)


async def extract_one_bid(
    document: Document,
) -> BidExtraction | None:
    """Run a single Sonnet 4.6 extraction call on one bid document."""
    client = _get_client()
    if client is None:
        log.warning("bid_extractor: no ANTHROPIC_API_KEY")
        return None

    from ..database import SessionLocal

    async with SessionLocal() as db:
        prompt_body, _ = await _gather_bid_text(db, document.id)

    if not prompt_body.strip():
        log.warning(
            "bid_extractor: no chunked text for bid %s (%s) — skipping",
            document.filename,
            document.id,
        )
        return None

    operator_vendor = document.vendor_name or "(not stated)"
    prompt = (
        f"Vendor at upload time (operator-provided, may differ from bid): "
        f"{operator_vendor}\n"
        f"Filename: {document.filename}\n\n"
        f"=== BID CONTENT ===\n{prompt_body}\n=== END BID ===\n\n"
        "Extract every priced line item, any stated inclusions, any stated "
        "exclusions, and the bid total. Use the extract_bid tool now."
    )

    t0 = time.perf_counter()
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_bid"},
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict | None = None
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "extract_bid"
        ):
            payload = block.input
            break
    if payload is None:
        log.warning("bid_extractor: no tool_use for bid %s", document.id)
        return None

    # Defensively coerce numeric fields. Sonnet sometimes returns the JSON
    # string "null" or "N/A" for nullable numbers; storing those into Float
    # columns 500s the run.
    def _coerce_float(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            cleaned = v.strip().replace(",", "").replace("$", "")
            if cleaned.lower() in {"", "null", "none", "n/a", "na", "tbd", "varies"}:
                return None
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    def _normalize_line(item: dict) -> dict:
        return {
            **item,
            "unit_price_usd": _coerce_float(item.get("unit_price_usd")),
            "total_price_usd": _coerce_float(item.get("total_price_usd")),
        }

    return BidExtraction(
        vendor_name=payload.get("vendor_name"),
        bid_total_usd=_coerce_float(payload.get("bid_total_usd")),
        primary_csi_divisions=list(payload.get("primary_csi_divisions") or []),
        line_items=[_normalize_line(it) for it in (payload.get("line_items") or [])],
        inclusions=list(payload.get("explicit_inclusions") or []),
        exclusions=list(payload.get("explicit_exclusions") or []),
        usage=usage_from_anthropic(msg),
        latency_ms=latency_ms,
    )


async def list_priced_bids(project_id: str) -> list[Document]:
    """Bids that have a chance of containing line items.

    We restrict to `bid-quote` + `scope-letter` doc_type — license/insurance/
    safety/contractor-info docs don't have line items so we don't waste a
    Sonnet call on them.
    """
    from ..database import SessionLocal

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Document)
                .where(Document.project_id == project_id)
                .where(Document.source == "bid_submission")
                .where(Document.doc_type.in_(_PRICED_BID_TYPES))
                .where(Document.processing_status == "ready")
            )
        ).scalars().all()
    return list(rows)


async def persist_bid_extraction(
    *,
    project_id: str,
    run_id: str,
    document: Document,
    extraction: BidExtraction,
) -> BidSummary:
    """Write line items, inclusions, exclusions, summary + record cost."""
    from ..database import SessionLocal

    async with SessionLocal() as db:
        cost, _ = await record_call(
            db,
            purpose="bid-extract",
            model="claude-sonnet-4-6",
            usage=extraction.usage,
            latency_ms=extraction.latency_ms,
            project_id=project_id,
            document_id=document.id,
        )
        for item in extraction.line_items:
            db.add(
                BidLineItem(
                    project_id=project_id,
                    run_id=run_id,
                    bid_document_id=document.id,
                    description=str(item.get("description") or "").strip(),
                    quantity=item.get("quantity"),
                    unit=item.get("unit"),
                    unit_price_usd=item.get("unit_price_usd"),
                    total_price_usd=item.get("total_price_usd"),
                    csi_section_guess=item.get("csi_section_guess"),
                    page_number=item.get("page_number"),
                )
            )
        for inc in extraction.inclusions:
            db.add(
                BidInclusion(
                    project_id=project_id,
                    run_id=run_id,
                    bid_document_id=document.id,
                    text=str(inc.get("text") or "").strip(),
                    page_number=inc.get("page_number"),
                )
            )
        for exc in extraction.exclusions:
            db.add(
                BidExclusion(
                    project_id=project_id,
                    run_id=run_id,
                    bid_document_id=document.id,
                    text=str(exc.get("text") or "").strip(),
                    page_number=exc.get("page_number"),
                )
            )

        summary = BidSummary(
            project_id=project_id,
            run_id=run_id,
            bid_document_id=document.id,
            vendor_name=extraction.vendor_name or document.vendor_name,
            bid_total_usd=extraction.bid_total_usd,
            primary_csi_divisions=extraction.primary_csi_divisions,
            line_item_count=len(extraction.line_items),
            inclusion_count=len(extraction.inclusions),
            exclusion_count=len(extraction.exclusions),
            extraction_cost_usd=cost or 0.0,
            extraction_latency_ms=extraction.latency_ms,
        )
        db.add(summary)
        await db.commit()
        await db.refresh(summary)
        return summary


async def extract_all_bids(
    project_id: str, run_id: str
) -> list[BidSummary]:
    """Extract every priced bid in the project. Returns the BidSummary rows."""
    bids = await list_priced_bids(project_id)
    if not bids:
        log.info("bid_extractor: no priced bids for project %s", project_id)
        return []

    log.info("bid_extractor: extracting %d bids for project %s", len(bids), project_id)
    sem = asyncio.Semaphore(_EXTRACT_CONCURRENCY)

    async def run_one(doc: Document) -> BidSummary | None:
        async with sem:
            try:
                ex = await extract_one_bid(doc)
                if ex is None:
                    return None
                return await persist_bid_extraction(
                    project_id=project_id,
                    run_id=run_id,
                    document=doc,
                    extraction=ex,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("bid_extractor: failed on %s: %s", doc.filename, e)
                return None

    summaries = await asyncio.gather(*(run_one(d) for d in bids))
    return [s for s in summaries if s is not None]
