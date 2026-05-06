"""Stage 8 — Settings + system status endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from ..config import settings as env_settings
from ..models import AuditLog, Document, LLMCall, Project, ScopeExtractionRun
from ..services import app_settings as app_settings_svc
from ..services.app_settings import (
    ALLOWED_SETTING_KEYS,
    SettingValidationError,
)
from .deps import DB, CurrentUser

router = APIRouter(prefix="/settings", tags=["settings"])


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
    overrides_in_use: list[str]


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


@router.get("", response_model=SettingsOut)
async def get_settings(db: DB, _: CurrentUser) -> SettingsOut:
    """Effective settings (env defaults overlaid with any DB overrides)."""
    overrides = await app_settings_svc.list_all(db)

    def eff(key: str, default: Any) -> Any:
        return overrides.get(key, default)

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
            "anthropic": bool(env_settings.anthropic_api_key),
            "cohere": bool(env_settings.cohere_api_key),
            "google": bool(env_settings.google_api_key),
            "openai": bool(env_settings.openai_api_key),
        },
        overrides_in_use=sorted(overrides.keys()),
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
        try:
            await app_settings_svc.set_setting(db, key, value)
        except SettingValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
            ) from e

    # Audit each change so the activity feed shows it
    if changes:
        for key, value in changes.items():
            db.add(
                AuditLog(
                    project_id="00000000-0000-0000-0000-000000000000",
                    entity_type="app_setting",
                    entity_id=key,
                    action="update",
                    actor=f"user:{user}",
                    payload={"new_value": value},
                )
            )
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
