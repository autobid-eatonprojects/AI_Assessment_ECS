"""Stage 8 — Settings + system status endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from ..config import settings as env_settings
from ..models import Document, LLMCall, Project, ScopeExtractionRun
from ..services import app_settings as app_settings_svc
from ..services.app_settings import (
    ALLOWED_SETTING_KEYS,
    API_KEY_SETTING_NAMES,
    SettingValidationError,
    mask_api_key,
)
from .deps import DB, CurrentUser

router = APIRouter(prefix="/settings", tags=["settings"])


# Where the user goes to actually create / find each provider's key.
# Surfaced in the GET response so the UI can render a "Get key" link
# next to each row instead of hard-coding URLs in the frontend.
PROVIDER_KEY_LINKS: dict[str, str] = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "voyage": "https://dash.voyageai.com/api-keys",
    "cohere": "https://dashboard.cohere.com/api-keys",
    "mistral": "https://console.mistral.ai/api-keys/",
    "google": "https://aistudio.google.com/apikey",
}


# Map env-settings field names to provider names used in the UI.
PROVIDER_TO_KEY_FIELD = {
    "anthropic": "anthropic_api_key",
    "voyage": "voyage_api_key",
    "cohere": "cohere_api_key",
    "mistral": "mistral_api_key",
    "google": "google_api_key",
}


class ProviderKeyInfo(BaseModel):
    """Per-provider info surfaced to the Settings UI.

    `masked` is the first 5 + last 4 chars of the configured key (or None
    if not set), so the UI can show "anthr…c4d2" without exposing the
    secret. `source` is "db" when the user set it from the UI (DB override
    in AppSetting) and "env" when it's still coming from backend/.env;
    "none" means no key is configured.
    """

    configured: bool
    masked: str | None = None
    source: str  # "db" | "env" | "none"
    signup_url: str


class SettingsOut(BaseModel):
    """Non-secret view of effective settings (env defaults + DB overrides)."""

    classifier_model: str
    vision_model: str
    vision_provider: str
    vision_concurrency: int
    embedding_model: str
    rerank_model: str
    contextualizer_model: str
    index_concurrency: int
    page_dpi: int
    thumbnail_max_dim: int
    default_theme: str
    # Boolean health: whether each provider key is configured (never the key itself)
    provider_keys_configured: dict[str, bool]
    # Per-provider detail (masked value + source + signup link)
    provider_keys: dict[str, ProviderKeyInfo]
    overrides_in_use: list[str]
    # API-key edits via the UI take effect on backend restart since the
    # provider clients are singletonized at process start.
    needs_restart_for: list[str]


class SettingsPatchIn(BaseModel):
    classifier_model: str | None = None
    vision_model: str | None = None
    vision_provider: str | None = None
    vision_concurrency: int | None = None
    embedding_model: str | None = None
    rerank_model: str | None = None
    contextualizer_model: str | None = None
    index_concurrency: int | None = None
    page_dpi: int | None = None
    thumbnail_max_dim: int | None = None
    default_theme: str | None = None
    bundling_rules_override_yaml: str | None = None
    trust_score_weights: dict | None = None
    # Provider API keys — empty string clears the DB override (falls back
    # to backend/.env value); a real value upserts the override.
    anthropic_api_key: str | None = None
    voyage_api_key: str | None = None
    cohere_api_key: str | None = None
    mistral_api_key: str | None = None
    google_api_key: str | None = None


class HealthOut(BaseModel):
    provider: str
    ok: bool
    detail: str | None = None


class SystemStatusOut(BaseModel):
    backend_version: str
    db_path: str
    project_count: int
    document_count: int
    scope_run_count: int
    llm_call_count: int
    total_cost_usd: float
    audit_log_count: int


def _env_key_for(provider: str) -> str | None:
    """Look up the env-settings value for a provider's API key."""
    field = PROVIDER_TO_KEY_FIELD.get(provider)
    if not field:
        return None
    return getattr(env_settings, field, None)


def _build_provider_keys(overrides: dict[str, Any]) -> dict[str, ProviderKeyInfo]:
    """For each provider, decide whether the effective key comes from a DB
    override, the env, or nowhere — and produce a masked display value.
    """
    out: dict[str, ProviderKeyInfo] = {}
    for provider, signup_url in PROVIDER_KEY_LINKS.items():
        field = PROVIDER_TO_KEY_FIELD.get(provider)
        if not field:
            continue
        db_value = overrides.get(field) if isinstance(overrides.get(field), str) else None
        env_value = _env_key_for(provider)
        if db_value:
            source = "db"
            effective = db_value
        elif env_value:
            source = "env"
            effective = env_value
        else:
            source = "none"
            effective = None
        out[provider] = ProviderKeyInfo(
            configured=effective is not None,
            masked=mask_api_key(effective),
            source=source,
            signup_url=signup_url,
        )
    return out


@router.get("", response_model=SettingsOut)
async def get_settings(db: DB, _: CurrentUser) -> SettingsOut:
    """Effective settings (env defaults overlaid with any DB overrides)."""
    overrides = await app_settings_svc.list_all(db)

    def eff(key: str, default: Any) -> Any:
        return overrides.get(key, default)

    provider_keys = _build_provider_keys(overrides)
    # Any provider whose key was just changed in the DB needs a backend
    # restart to take effect (clients are singletonized at process start).
    needs_restart = [
        provider
        for provider, info in provider_keys.items()
        if info.source == "db"
    ]

    return SettingsOut(
        classifier_model=eff("classifier_model", env_settings.classifier_model),
        vision_model=eff("vision_model", env_settings.vision_model),
        vision_provider=eff("vision_provider", env_settings.vision_provider),
        vision_concurrency=eff("vision_concurrency", env_settings.vision_concurrency),
        embedding_model=eff("embedding_model", env_settings.embedding_model),
        rerank_model=eff("rerank_model", env_settings.rerank_model),
        contextualizer_model=eff(
            "contextualizer_model", env_settings.contextualizer_model
        ),
        index_concurrency=eff("index_concurrency", env_settings.index_concurrency),
        page_dpi=eff("page_dpi", env_settings.page_dpi),
        thumbnail_max_dim=eff("thumbnail_max_dim", env_settings.thumbnail_max_dim),
        default_theme=eff("default_theme", "system"),
        provider_keys_configured={
            p: info.configured for p, info in provider_keys.items()
        },
        provider_keys=provider_keys,
        # Sort overrides for stable output, but redact API key names so
        # the UI doesn't surface that the key exists in DB through this
        # field (the masked-value path is the canonical view).
        overrides_in_use=sorted(
            k for k in overrides.keys() if k not in API_KEY_SETTING_NAMES
        ),
        needs_restart_for=sorted(needs_restart),
    )


@router.patch("", response_model=SettingsOut)
async def patch_settings(
    payload: SettingsPatchIn, db: DB, user: CurrentUser
) -> SettingsOut:
    """Upsert a subset of settings. Returns the updated effective view."""
    changes = {
        k: v
        for k, v in payload.model_dump(exclude_none=True).items()
        if k in ALLOWED_SETTING_KEYS
    }
    for key, value in changes.items():
        # API keys: empty string clears the DB override (revert to env).
        if key in API_KEY_SETTING_NAMES and isinstance(value, str) and value == "":
            await app_settings_svc.clear_setting(db, key)
            continue
        try:
            await app_settings_svc.set_setting(db, key, value)
        except SettingValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
            ) from e

    # AuditLog is project-scoped (project_id is non-nullable + FK to
    # projects.id). App-level settings have no project context so we
    # don't write audit rows for them — the AppSetting row itself is
    # the source of truth + the patched value goes through the normal
    # response flow. (API keys are masked in the response, never echoed.)
    await db.commit()
    return await get_settings(db, user)


@router.get("/health/{provider}", response_model=HealthOut)
async def provider_health(
    provider: str, _db: DB, _: CurrentUser
) -> HealthOut:
    """Trivial reachability check for a configured provider key.

    Does NOT make a paid API call — just confirms a key exists. A real ping
    can be added per provider later (e.g., Anthropic ``client.messages.count``,
    Cohere ``client.tokenize``).
    """
    keys = {
        "anthropic": env_settings.anthropic_api_key,
        "cohere": env_settings.cohere_api_key,
        "google": env_settings.google_api_key,
        "openai": env_settings.openai_api_key,
    }
    if provider not in keys:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown provider {provider}",
        )
    if not keys[provider]:
        return HealthOut(provider=provider, ok=False, detail="key not configured")
    return HealthOut(provider=provider, ok=True, detail="key present")


@router.get("/status", response_model=SystemStatusOut)
async def system_status(db: DB, _: CurrentUser) -> SystemStatusOut:
    project_count = (
        await db.execute(select(func.count()).select_from(Project))
    ).scalar() or 0
    document_count = (
        await db.execute(select(func.count()).select_from(Document))
    ).scalar() or 0
    scope_run_count = (
        await db.execute(select(func.count()).select_from(ScopeExtractionRun))
    ).scalar() or 0
    llm_call_count = (
        await db.execute(select(func.count()).select_from(LLMCall))
    ).scalar() or 0
    total_cost = (
        await db.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)))
    ).scalar() or 0.0
    audit_log_count = (
        await db.execute(select(func.count()).select_from(AuditLog))
    ).scalar() or 0

    return SystemStatusOut(
        backend_version="0.1.0",
        db_path=str(env_settings.database_url),
        project_count=int(project_count),
        document_count=int(document_count),
        scope_run_count=int(scope_run_count),
        llm_call_count=int(llm_call_count),
        total_cost_usd=float(total_cost),
        audit_log_count=int(audit_log_count),
    )
