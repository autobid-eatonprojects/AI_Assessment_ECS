"""Phase 6 — Quantity Resolver (Stage E).

Deterministic post-processing pass that runs after dedupe + persistence.
For each ScopeItem, gather every quantity signal available — Sonnet-stated
in the EVE extraction, aggregated counts from schedule-miner clusters,
explicit numerics regex-sniffed from citation excerpts — and resolve a
final `quantity` + `unit` + `qty_confidence` + `qty_provenance` audit blob.

We deliberately skip plan-derived scale measurement (drawing scale ×
bbox area → SF). Reading the scale bar from a drawing is brittle and
the failure modes are silent — we'd rather honestly mark items
`unverified` than fabricate a number.

Cost: 0 (no LLM calls). Latency: <1s for the full Elks set (~600 items).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ScopeCitation, ScopeItem

log = logging.getLogger(__name__)


# Regex sniffer for quantity-like strings in citation excerpts.
# Order matters — we prefer compound matches ("5,200 SF") over bare numbers.
# The (?=...) lookahead at the end stops "5 each" from matching as "5 EA"
# (bare alphabetic continuation is not a unit-token boundary).
_QTY_PATTERN = re.compile(
    r"""
    (?<![\w.])                     # no alpha/digit/dot immediately before
    (?P<num>
        \d{1,3}(?:,\d{3})+        # thousands-separated: 5,200
        |
        \d+(?:\.\d+)?              # plain int or decimal: 12.5
    )
    \s*
    (?P<unit>
        SF|sq\.?\s*ft\.?|S\.?F\.?
        |LF|lin\.?\s*ft\.?|L\.?F\.?
        |CY|cu\.?\s*yd\.?|C\.?Y\.?
        |EA|ea\.?
        |lbs?|pounds?
        |TON|tons?
        |GAL|gal\.?
    )
    (?![A-Za-z])                   # the unit token must end at a non-letter
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Canonicalise unit aliases so two sources stating "SF" and "sq.ft." don't
# look like a conflict.
_UNIT_CANONICAL: dict[str, str] = {}
for canon, aliases in {
    "SF": ["SF", "sq.ft.", "sq ft", "S.F.", "sqft"],
    "LF": ["LF", "lin.ft.", "lin ft", "L.F.", "linft"],
    "CY": ["CY", "cu.yd.", "cu yd", "C.Y.", "cuyd"],
    "EA": ["EA", "ea.", "ea"],
    "LB": ["lbs", "lb", "pounds", "pound"],
    "TON": ["TON", "tons", "ton"],
    "GAL": ["GAL", "gal.", "gal"],
}.items():
    for a in aliases:
        _UNIT_CANONICAL[a.lower().replace(" ", "").replace(".", "")] = canon


def _canon_unit(u: str | None) -> str | None:
    if not u:
        return None
    key = u.lower().replace(" ", "").replace(".", "")
    return _UNIT_CANONICAL.get(key, u.upper())


def _parse_qty_text(qty_text: str | None) -> tuple[float | None, str | None]:
    """Try to read a qty out of a free-text quantity field. Returns (value, raw_unit_token)."""
    if not qty_text:
        return None, None
    s = qty_text.strip()
    m = _QTY_PATTERN.search(s)
    if m:
        n = float(m.group("num").replace(",", ""))
        return n, m.group("unit")
    # Bare number (no unit attached in the qty string itself)
    bare = re.match(r"^\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*$", s)
    if bare:
        return float(bare.group(1).replace(",", "")), None
    return None, None


def _sniff_excerpt(excerpt: str | None) -> list[tuple[float, str]]:
    """Return all (value, canonical_unit) pairs found in a citation excerpt."""
    if not excerpt:
        return []
    out: list[tuple[float, str]] = []
    for m in _QTY_PATTERN.finditer(excerpt):
        n = float(m.group("num").replace(",", ""))
        unit = _canon_unit(m.group("unit")) or ""
        if unit:
            out.append((n, unit))
    return out


@dataclass
class _QtyEvidence:
    """One quantity signal from one source."""

    value: float
    unit: str
    source: str  # "sonnet_stated" | "schedule_miner_count" | "excerpt_regex"
    detail: str | None = None  # e.g. citation chunk_id or "stated qty"


def _resolve_for_item(
    item: ScopeItem,
    citations: list[ScopeCitation],
) -> tuple[str | None, str | None, str, dict]:
    """Return (final_qty_str, final_unit, confidence_band, provenance_dict)."""
    evidence: list[_QtyEvidence] = []

    # 1. Stated quantity on the ScopeItem itself (Sonnet EVE or
    #    schedule_miner per-row default of 1 EA).
    stated_val, stated_unit_raw = _parse_qty_text(item.quantity)
    stated_unit = _canon_unit(stated_unit_raw) or _canon_unit(item.unit)
    if stated_val is not None and stated_unit:
        source = (
            "schedule_miner_stated"
            if item.extraction_method == "schedule_miner"
            else "sonnet_stated"
        )
        evidence.append(_QtyEvidence(stated_val, stated_unit, source, item.quantity))

    # 2. Excerpt regex: sniff numerics from citation excerpts. Only counted
    #    as additional evidence when it agrees with (or contradicts) the
    #    stated qty — the resolver doesn't promote a regex match over a
    #    Sonnet-stated qty unless they share the same unit class.
    for c in citations:
        for val, unit in _sniff_excerpt(c.excerpt):
            evidence.append(
                _QtyEvidence(
                    val, unit, "excerpt_regex", f"chunk {c.chunk_id}"
                )
            )

    if not evidence:
        return item.quantity, item.unit, "unverified", {
            "reason": "no quantity signal in any source",
        }

    # Resolve: pick the highest-priority signal, but flag conflicts where
    # multiple sources disagree on the same unit-class.
    by_unit: dict[str, list[_QtyEvidence]] = defaultdict(list)
    for e in evidence:
        by_unit[e.unit].append(e)

    # Priority: schedule_miner_stated > sonnet_stated > excerpt_regex.
    priority = {
        "schedule_miner_stated": 0,
        "sonnet_stated": 1,
        "excerpt_regex": 2,
    }

    def _best(es: list[_QtyEvidence]) -> _QtyEvidence:
        return min(es, key=lambda e: priority.get(e.source, 99))

    chosen_unit, chosen_evs = max(
        by_unit.items(), key=lambda kv: -priority.get(_best(kv[1]).source, 99)
    )
    chosen = _best(chosen_evs)

    # Conflict detection: same unit, multiple distinct values >5% apart
    distinct = sorted({round(e.value, 2) for e in chosen_evs})
    if len(distinct) > 1:
        spread = (max(distinct) - min(distinct)) / max(min(distinct), 1)
        if spread > 0.05:
            return (
                str(chosen.value).rstrip("0").rstrip("."),
                chosen_unit,
                "conflicting",
                {
                    "chosen": {
                        "value": chosen.value,
                        "unit": chosen_unit,
                        "source": chosen.source,
                    },
                    "all_values": [
                        {"value": e.value, "unit": e.unit, "source": e.source}
                        for e in evidence
                    ],
                    "spread_pct": round(spread * 100, 1),
                },
            )

    # Confidence band
    if chosen.source == "schedule_miner_stated":
        band = "high"
    elif chosen.source == "sonnet_stated":
        band = "high" if len(evidence) > 1 else "medium"
    else:
        band = "medium"

    # Render qty: integer if whole, else stripped decimal
    qty_str = (
        str(int(chosen.value))
        if chosen.value == int(chosen.value)
        else f"{chosen.value:.2f}".rstrip("0").rstrip(".")
    )
    return (
        qty_str,
        chosen_unit,
        band,
        {
            "chosen": {
                "value": chosen.value,
                "unit": chosen_unit,
                "source": chosen.source,
                "detail": chosen.detail,
            },
            "evidence_count": len(evidence),
        },
    )


async def resolve_quantities(run_id: str) -> int:
    """Walk every ScopeItem in the run and resolve final quantities.

    Returns: number of items updated. Persists qty_confidence + qty_provenance
    + may overwrite quantity / unit when a higher-priority signal exists.
    """
    from ..database import SessionLocal

    async with SessionLocal() as db:
        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        if not items:
            return 0

        # Pull citations once, group by scope_item_id
        citations_rows = (
            await db.execute(
                select(ScopeCitation).where(
                    ScopeCitation.scope_item_id.in_([i.id for i in items])
                )
            )
        ).scalars().all()
        by_item: dict[str, list[ScopeCitation]] = defaultdict(list)
        for c in citations_rows:
            by_item[c.scope_item_id].append(c)

        updated = 0
        for item in items:
            qty, unit, band, prov = _resolve_for_item(
                item,
                by_item.get(item.id, []),
            )
            changed = (
                item.qty_confidence != band
                or item.quantity != qty
                or item.unit != unit
            )
            item.quantity = qty
            item.unit = unit
            item.qty_confidence = band
            item.qty_provenance = prov
            if changed:
                updated += 1
        await db.commit()

    log.info(
        "quantity_resolver: run %s — %d/%d items updated",
        run_id,
        updated,
        len(items),
    )
    return updated
