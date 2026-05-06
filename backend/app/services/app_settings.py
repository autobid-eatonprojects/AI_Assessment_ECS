"""Stage 8 — read/write hot-reloadable app settings.

Pattern:
    get_setting(key, default) reads the AppSetting row, falling back to the
    provided default (typically the value from app.config.settings).

    set_setting(key, value) upserts.

The Settings UI uses these to override model IDs and concurrency limits
without restarting the backend.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppSetting


# Whitelist of keys the Settings UI may write. Prevents arbitrary write
# from leaking sensitive surface area; new keys land here intentionally.
ALLOWED_SETTING_KEYS = {
    "classifier_model",
    "vision_model",
    "vision_provider",
    "vision_concurrency",
    "embedding_model",
    "rerank_model",
    "contextualizer_model",
    "index_concurrency",
    "trust_score_weights",
    "page_dpi",
    "thumbnail_max_dim",
    # Bundling rules YAML override (string content, parsed by bundling_rules.py
    # if/when we wire DB-backed rules into the loader)
    "bundling_rules_override_yaml",
    # UI preferences
    "default_theme",
}


class SettingValidationError(ValueError):
    pass


async def get_setting(
    db: AsyncSession, key: str, default: Any = None
) -> Any:
    row = await db.get(AppSetting, key)
    if row is None or row.value_json is None:
        return default
    payload = row.value_json
    return payload.get("value", default) if isinstance(payload, dict) else payload


async def set_setting(db: AsyncSession, key: str, value: Any) -> AppSetting:
    if key not in ALLOWED_SETTING_KEYS:
        raise SettingValidationError(
            f"setting key {key!r} not in ALLOWED_SETTING_KEYS"
        )
    row = await db.get(AppSetting, key)
    payload = {"value": value}
    if row is None:
        row = AppSetting(key=key, value_json=payload)
        db.add(row)
    else:
        row.value_json = payload
    await db.flush()
    return row


async def list_all(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(select(AppSetting))).scalars().all()
    out: dict[str, Any] = {}
    for r in rows:
        if r.value_json is None:
            continue
        out[r.key] = (
            r.value_json.get("value")
            if isinstance(r.value_json, dict) and "value" in r.value_json
            else r.value_json
        )
    return out
