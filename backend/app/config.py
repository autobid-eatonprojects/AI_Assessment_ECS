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

    # Postgres 16+ with the pgvector extension — single store for relational
    # rows + ACID joins to embedding vectors. Override via DATABASE_URL env
    # to point at a managed Postgres (Supabase / RDS / Neon) in production.
    database_url: str = "postgresql+asyncpg://nvp@localhost:5432/ecs_estimator"
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

    # Vision pre-pass (Phase 2) — primary stays Anthropic; Google is a
    # configurable alternative (benchmarks show Gemini 2.5 Pro stronger on
    # engineering drawings). Set vision_provider="google" to switch.
    vision_provider: str = "anthropic"  # anthropic | google
    vision_model: str = "claude-sonnet-4-6"
    vision_concurrency: int = 5  # parallel vision calls
    vision_max_retries: int = 2

    # Google Gemini
    google_api_key: str | None = None
    gemini_vision_model: str = "gemini-2.5-pro"

    # Mistral OCR — primary OCR per design doc P1, best AEC accuracy.
    # When set, the OCR ensemble runs Mistral + Gemini in parallel and
    # picks the higher-quality output (and flags pages with significant
    # disagreement for human review). Without it, falls back to Gemini only.
    mistral_api_key: str | None = None
    mistral_ocr_model: str = "mistral-ocr-latest"

    # YOLO11 MEP symbol pre-pass (W3 mitigation). Set to a path-on-disk
    # of a fine-tuned YOLO model (typically `best.pt` from Roboflow or
    # custom training). When set AND `ultralytics` is installed, the
    # discipline_agent enriches FP/P/M/E context with structured symbol
    # detections. When unset, those agents fall back to vision-only.
    yolo_mep_model_path: str | None = None

    # Scope orchestration mode (P3/P4 of design doc).
    #   "per-discipline" — NEW. Runs one discipline_agent per
    #     architectural / structural / mechanical / etc. bucket. Each
    #     agent reads its own ~80K token corpus once (cached) and emits
    #     bilateral-evidence scope_items in a single Sonnet call.
    #     Cheaper, more coherent (mandates ↔ work_items cross-checked
    #     in-context), and cuts call count from ~30 divisions × 3
    #     queries × 3 votes ≈ 270 calls down to ~10 disciplines.
    #   "per-division" — LEGACY. Existing per-CSI-division Stage A/B/C/D
    #     pipeline. Kept for fallback + diff comparison so we can A/B
    #     output quality before deprecating.
    scope_orchestration_mode: str = "per-discipline"

    # Phase 3 — Indexing + Retrieval
    # Embeddings: voyage-3-large (1024d) per the design doc — best published
    # AEC retrieval scores. Cohere Rerank 3 still does cross-encoder rerank.
    openai_api_key: str | None = None  # reserved for future / fallback
    voyage_api_key: str | None = None
    embedding_model: str = "voyage-3-large"
    embedding_dim: int = 1024

    cohere_api_key: str | None = None
    rerank_model: str = "rerank-v3.5"

    contextualizer_model: str = "claude-haiku-4-5"
    index_concurrency: int = 10

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
