"""P7 — quantity sanity heuristics (W7 mitigation).

After quantity_resolver populates qty_value + qty_uom, this pass flags
items whose numeric quantity is implausible for the unit + CSI division.
Common failure modes this catches:

  - Unit mismatch from the agent: "5,200 SF" attributed to a fixture
    schedule that should be "5,200 EA" — wildly off in either reading
  - Decimal-shift typo: "0.45 SF" of slab on grade (should be 4,500)
  - Unit category mismatch: a Div 22 Water Closet item with qty_uom='SF'
  - Conflicting evidence already band-flagged → upgrade to a hard
    "needs_review" sanity flag

Pure rules; no LLM call. Surfaces results as Gap rows of type
'qty_implausible' so the HITL queue picks them up.

Per the design doc, this complements (not replaces) the Sonnet 4.6
verifier — verifier handles ambiguous semantic cases, sanity catches
the deterministic "this number is impossible" ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from ..database import SessionLocal
from ..models import Gap, ScopeItem

log = logging.getLogger(__name__)


# Plausible (min, max) ranges per (csi_division, qty_uom) pair.
# Tuned to flag obvious typos (decimal shift, unit confusion) without
# false-positive on legit edge cases. Conservative on the upper bound
# so genuinely large jobs don't trigger.
#
# A None on either bound means "no limit on that side" — typically
# used when the lower bound is meaningless (qty=0 is impossible
# everywhere already filtered).
_RANGES: dict[tuple[str, str], tuple[float | None, float | None]] = {
    # Concrete (Div 03)
    ("03", "CY"): (0.5, 50_000.0),
    ("03", "SF"): (10.0, 500_000.0),
    ("03", "LF"): (1.0, 50_000.0),
    # Masonry (Div 04)
    ("04", "SF"): (10.0, 200_000.0),
    ("04", "EA"): (1.0, 100_000.0),
    # Metals (Div 05)
    ("05", "TON"): (0.1, 5_000.0),
    ("05", "LB"): (10.0, 5_000_000.0),
    ("05", "LF"): (1.0, 50_000.0),
    # Wood (Div 06)
    ("06", "SF"): (10.0, 200_000.0),
    ("06", "LF"): (1.0, 100_000.0),
    # Thermal/Moisture (Div 07)
    ("07", "SF"): (10.0, 500_000.0),
    ("07", "LF"): (1.0, 50_000.0),
    # Openings (Div 08) — count units, low ceiling
    ("08", "EA"): (1.0, 5_000.0),
    ("08", "SF"): (1.0, 50_000.0),
    # Finishes (Div 09)
    ("09", "SF"): (10.0, 1_000_000.0),
    ("09", "LF"): (1.0, 100_000.0),
    ("09", "EA"): (1.0, 100_000.0),
    # Specialties (Div 10) — count items
    ("10", "EA"): (1.0, 10_000.0),
    # Fire suppression (Div 21)
    ("21", "EA"): (1.0, 50_000.0),
    ("21", "LF"): (1.0, 100_000.0),
    # Plumbing (Div 22)
    ("22", "EA"): (1.0, 5_000.0),
    ("22", "LF"): (1.0, 100_000.0),
    ("22", "GAL"): (1.0, 100_000.0),
    # HVAC (Div 23)
    ("23", "EA"): (1.0, 5_000.0),
    ("23", "LF"): (1.0, 100_000.0),
    ("23", "TON"): (0.5, 10_000.0),
    # Electrical (Div 26)
    ("26", "EA"): (1.0, 100_000.0),
    ("26", "LF"): (1.0, 1_000_000.0),
    # Earthwork (Div 31)
    ("31", "CY"): (1.0, 1_000_000.0),
    ("31", "SF"): (10.0, 5_000_000.0),
    ("31", "LF"): (1.0, 200_000.0),
    # Exterior improvements (Div 32)
    ("32", "SF"): (10.0, 5_000_000.0),
    ("32", "LF"): (1.0, 500_000.0),
    ("32", "EA"): (1.0, 50_000.0),
    # Utilities (Div 33)
    ("33", "LF"): (1.0, 1_000_000.0),
    ("33", "EA"): (1.0, 50_000.0),
}


# Hard mismatches: certain (csi_division, qty_uom) pairs are categorically
# wrong. A Water Closet measured in SF is a unit-category error, not just
# an out-of-range number. We use a separate enum so the Gap message can be
# more pointed ("expected EA, got SF").
_EXPECTED_UOM_BY_DIV: dict[str, set[str]] = {
    "03": {"CY", "SF", "LF"},
    "04": {"SF", "EA"},
    "05": {"TON", "LB", "LF", "EA"},
    "06": {"SF", "LF", "EA"},
    "07": {"SF", "LF"},
    "08": {"EA", "SF"},
    "09": {"SF", "LF", "EA"},
    "10": {"EA"},
    "21": {"EA", "LF"},
    "22": {"EA", "LF", "GAL"},
    "23": {"EA", "LF", "TON"},
    "26": {"EA", "LF"},
    "31": {"CY", "SF", "LF"},
    "32": {"SF", "LF", "EA"},
    "33": {"LF", "EA"},
}


@dataclass
class _SanityFinding:
    item_id: str
    csi_code: str
    description: str
    qty_value: float
    qty_uom: str
    severity: str  # warn | blocker
    reason: str


def _check_one(item: ScopeItem) -> _SanityFinding | None:
    """Return a finding if the item's quantity violates a sanity rule."""
    if item.qty_value is None or item.qty_uom is None:
        return None
    val, uom = item.qty_value, item.qty_uom

    # Hard category mismatch — wrong unit category for the division
    expected = _EXPECTED_UOM_BY_DIV.get(item.csi_division)
    if expected and uom not in expected:
        return _SanityFinding(
            item_id=item.id,
            csi_code=item.csi_code,
            description=item.description,
            qty_value=val,
            qty_uom=uom,
            severity="blocker",
            reason=(
                f"Div {item.csi_division} items are typically measured in "
                f"{sorted(expected)}; got '{uom}'. Probable unit-category "
                f"misread; verify against source."
            ),
        )

    # Range check
    rng = _RANGES.get((item.csi_division, uom))
    if rng is None:
        return None  # No tuned range → don't flag
    lo, hi = rng
    if lo is not None and val < lo:
        return _SanityFinding(
            item_id=item.id,
            csi_code=item.csi_code,
            description=item.description,
            qty_value=val,
            qty_uom=uom,
            severity="warn",
            reason=(
                f"qty {val:g} {uom} is below typical lower bound {lo:g} for "
                f"Div {item.csi_division}. Probable decimal-shift typo or "
                f"wrong unit."
            ),
        )
    if hi is not None and val > hi:
        return _SanityFinding(
            item_id=item.id,
            csi_code=item.csi_code,
            description=item.description,
            qty_value=val,
            qty_uom=uom,
            severity="warn",
            reason=(
                f"qty {val:g} {uom} exceeds typical upper bound {hi:g} for "
                f"Div {item.csi_division}. Probable typo or unit confusion."
            ),
        )

    return None


async def check_run(run_id: str) -> dict:
    """Validate quantity sanity for every item in a run.

    Persists Gap rows of type 'qty_implausible' for each finding.
    Idempotent: deletes prior qty_implausible gaps for this run before
    inserting fresh ones.
    """
    findings: list[_SanityFinding] = []
    async with SessionLocal() as db:
        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        for item in items:
            finding = _check_one(item)
            if finding is not None:
                findings.append(finding)

        # Wipe + re-insert for idempotency
        await db.execute(
            Gap.__table__.delete().where(
                Gap.run_id == run_id,
            ).where(Gap.gap_type == "qty_implausible")
        )
        if not findings:
            await db.commit()
            log.info("quantity_sanity: run %s — no findings", run_id)
            return {"checked": len(items), "findings": 0}

        # Need project_id from one of the items
        project_id = items[0].project_id if items else None
        for f in findings:
            db.add(
                Gap(
                    project_id=project_id,
                    run_id=run_id,
                    gap_type="qty_implausible",
                    csi_division=(f.csi_code[:2] or None),
                    csi_section=f.csi_code,
                    description=(
                        f"[qty-sanity] {f.description} — {f.qty_value:g} "
                        f"{f.qty_uom} — {f.reason}"
                    ),
                    severity=f.severity,
                    suggested_remediation=(
                        "Open the source citation and verify the quantity "
                        "against the spec / drawing. Update via HITL if "
                        "the auto-extracted value is wrong."
                    ),
                    related_item_id=f.item_id,
                )
            )
        await db.commit()

    blockers = sum(1 for f in findings if f.severity == "blocker")
    warns = sum(1 for f in findings if f.severity == "warn")
    log.info(
        "quantity_sanity: run %s — %d findings (%d blocker, %d warn)",
        run_id, len(findings), blockers, warns,
    )
    return {
        "checked": len(items),
        "findings": len(findings),
        "blockers": blockers,
        "warnings": warns,
    }
