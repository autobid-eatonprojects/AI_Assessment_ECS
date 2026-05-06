"""Stage 4 — load + resolve bundling rules.

Combines the system-wide YAML default (``app/data/bundling_rules.yaml``)
with per-project overrides (TradePackage rows where
``bundling_rule_source='override'``) into a single resolved mapping:

    division_code → PackageRule

Errors fast on duplicate division mappings within either source.
Overrides take precedence over the YAML for any divisions they claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import TradePackage

log = logging.getLogger(__name__)


_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "bundling_rules.yaml"


@dataclass(frozen=True)
class PackageRule:
    """One trade package with the divisions it owns."""

    key: str
    label: str
    divisions: tuple[str, ...]
    source: str  # "yaml" | "override"


class BundlingRulesError(ValueError):
    """Raised when YAML or overrides have duplicate or malformed mappings."""


def _load_yaml() -> list[PackageRule]:
    if not _RULES_PATH.exists():
        log.warning("bundling_rules: %s not found; returning empty default", _RULES_PATH)
        return []
    with _RULES_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw_packages = data.get("packages") or []
    rules: list[PackageRule] = []
    for entry in raw_packages:
        key = (entry.get("key") or "").strip()
        label = (entry.get("label") or "").strip()
        divisions_raw = entry.get("divisions") or []
        if not key or not label or not divisions_raw:
            raise BundlingRulesError(
                f"bundling_rules: malformed entry {entry!r}; need key + label + divisions"
            )
        divisions = tuple(str(d).strip().zfill(2) for d in divisions_raw)
        rules.append(PackageRule(key=key, label=label, divisions=divisions, source="yaml"))
    return rules


async def _load_overrides(db: AsyncSession, project_id: str) -> list[PackageRule]:
    """Pull TradePackage rows tagged as project overrides.

    Override rows are project-scoped and have bundling_rule_source='override'
    plus their own ``csi_divisions`` JSON list. They get checked first so an
    override row claiming Div 03 wins over the YAML's concrete-masonry rule.
    """
    rows = (
        await db.execute(
            select(TradePackage)
            .where(TradePackage.project_id == project_id)
            .where(TradePackage.bundling_rule_source == "override")
        )
    ).scalars().all()
    return [
        PackageRule(
            key=r.package_key,
            label=r.package_label,
            divisions=tuple(d for d in (r.csi_divisions or [])),
            source="override",
        )
        for r in rows
    ]


def _build_mapping(rules: list[PackageRule]) -> dict[str, PackageRule]:
    """Flatten rules into a division→rule map. Detects double-claims."""
    mapping: dict[str, PackageRule] = {}
    for rule in rules:
        for div in rule.divisions:
            if div in mapping and mapping[div].key != rule.key:
                raise BundlingRulesError(
                    f"bundling_rules: division {div} claimed by both "
                    f"{mapping[div].key!r} and {rule.key!r}"
                )
            mapping[div] = rule
    return mapping


async def get_bundling_rules(
    db: AsyncSession, project_id: str
) -> tuple[list[PackageRule], dict[str, PackageRule]]:
    """Resolve effective bundling rules for a project.

    Returns ``(unique_rules, division_to_rule_map)``. Override rules take
    precedence over the YAML defaults for any divisions they claim.
    """
    yaml_rules = _load_yaml()
    overrides = await _load_overrides(db, project_id)

    # Overrides claim divisions first, then YAML fills the rest.
    override_map = _build_mapping(overrides)
    yaml_filtered: list[PackageRule] = []
    for rule in yaml_rules:
        keep_divs = tuple(d for d in rule.divisions if d not in override_map)
        if keep_divs:
            yaml_filtered.append(
                PackageRule(
                    key=rule.key,
                    label=rule.label,
                    divisions=keep_divs,
                    source="yaml",
                )
            )

    final_rules = overrides + yaml_filtered
    final_map = _build_mapping(final_rules)
    return final_rules, final_map
