"""Stage 8 — hot-reloadable app settings (model overrides, concurrency,
trust-score weights). Single key/value JSON store.

Why a DB table instead of env vars: model overrides + bundling weight
tweaks should be editable in the UI without a backend restart. Env vars
require a restart and a developer.

The Settings page in the UI reads/writes this table. Application code
falls back to ``app.config.settings`` (env-derived defaults) when a key
is absent here.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
