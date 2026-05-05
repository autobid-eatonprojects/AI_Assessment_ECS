from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # Empty shell env vars must not override .env values
    )

    app_env: str = "development"
    app_port: int = 8000

    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    storage_root: Path = Path("./data/uploads")

    dev_user_email: str = "admin@ecs.local"
    dev_user_password: str = "admin"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 1440

    cors_origins: str = "http://localhost:3000"

    max_upload_mb: int = 200

    # AI providers
    anthropic_api_key: str | None = None
    classifier_model: str = "claude-haiku-4-5"

    # Vision pre-pass (Phase 2)
    vision_model: str = "claude-sonnet-4-6"
    vision_concurrency: int = 5  # parallel Claude vision calls
    vision_max_retries: int = 2

    # PDF rendering
    page_dpi: int = 150  # full-page render DPI
    thumbnail_max_dim: int = 320  # px

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


settings = Settings()
settings.storage_root.mkdir(parents=True, exist_ok=True)
