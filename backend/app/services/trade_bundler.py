"""Stage 4 — assign every ScopeItem in a run to a TradePackage.

For each unique CSI division present in the run's scope items, look up the
applicable PackageRule (from `bundling_rules.get_bundling_rules`). Create
a TradePackage row per (run, package_key), then assign every item via
TradePackageItem rows + ``ScopeItem.package_id``.

Items in divisions not covered by any rule get a synthesized package
``unbundled-NN`` so nothing is silently dropped.

Pure SQL — no LLM calls. Re-runnable: deletes any prior TradePackage rows
for the run before re-creating, so a manual "rebundle" UI button can call
this without orphaning state.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import delete, select

from ..database import SessionLocal
from ..models import ScopeItem, TradePackage, TradePackageItem
from .bundling_rules import PackageRule, get_bundling_rules

log = logging.getLogger(__name__)


@dataclass
class BundleStats:
    packages_created: int
    items_assigned: int
    unbundled_items: int


def _unbundled_rule(division: str) -> PackageRule:
    """Synthesize a package rule for items in unmapped divisions."""
    return PackageRule(
        key=f"unbundled-{division}",
        label=f"Unbundled — Division {division}",
        divisions=(division,),
        source="yaml",
    )


async def bundle_run(run_id: str) -> BundleStats:
    """Assign every ScopeItem in a run to a TradePackage. Returns stats."""
    async with SessionLocal() as db:
        items = (
            await db.execute(
                select(ScopeItem).where(ScopeItem.run_id == run_id)
            )
        ).scalars().all()
        if not items:
            return BundleStats(0, 0, 0)
        project_id = items[0].project_id

        # Wipe prior packages for this run so re-bundling is idempotent.
        # (TradePackageItem cascades via FK; ScopeItem.package_id is loose
        # and reset below.)
        await db.execute(
            delete(TradePackage).where(TradePackage.run_id == run_id)
        )
        await db.flush()

        _, division_to_rule = await get_bundling_rules(db, project_id)

        # Bucket items by their effective rule (synth one for unmapped divs).
        items_by_rule: dict[str, tuple[PackageRule, list[ScopeItem]]] = {}
        for item in items:
            rule = division_to_rule.get(item.csi_division) or _unbundled_rule(
                item.csi_division
            )
            entry = items_by_rule.get(rule.key)
            if entry is None:
                items_by_rule[rule.key] = (rule, [item])
            else:
                entry[1].append(item)

        unbundled_items = 0
        packages_created = 0
        items_assigned = 0

        for rule, member_items in items_by_rule.values():
            bilateral_count = sum(
                1 for i in member_items if i.bilateral_evidence
            )
            confidences = [i.confidence for i in member_items if i.confidence is not None]
            avg_conf = sum(confidences) / len(confidences) if confidences else None

            pkg = TradePackage(
                project_id=project_id,
                run_id=run_id,
                package_key=rule.key,
                package_label=rule.label,
                csi_divisions=list(rule.divisions),
                bundling_rule_source=rule.source,
                item_count=len(member_items),
                bilateral_count=bilateral_count,
                avg_confidence=avg_conf,
            )
            db.add(pkg)
            await db.flush()
            packages_created += 1

            for item in member_items:
                db.add(
                    TradePackageItem(
                        package_id=pkg.id,
                        scope_item_id=item.id,
                        assignment_method="primary_division",
                    )
                )
                # Refresh ScopeItem with package_id
                merged = await db.get(ScopeItem, item.id)
                if merged is not None:
                    merged.package_id = pkg.id
                items_assigned += 1
                if rule.key.startswith("unbundled-"):
                    unbundled_items += 1

        await db.commit()

    log.info(
        "trade_bundler: run %s — %d packages, %d items assigned (%d unbundled)",
        run_id,
        packages_created,
        items_assigned,
        unbundled_items,
    )
    return BundleStats(
        packages_created=packages_created,
        items_assigned=items_assigned,
        unbundled_items=unbundled_items,
    )
