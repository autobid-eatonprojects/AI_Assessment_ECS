import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import (
    audit,
    auth,
    bids,
    documents,
    extraction,
    outputs,
    packages,
    profile,
    projects,
    review,
    scope,
    search,
    settings as settings_api,
    trade_relevance,
    vendors,
)
from .config import settings
from .database import init_db

# App-level loggers (processor, classifier, renderer) need INFO level to be
# visible alongside uvicorn access logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Resume any work that was in flight when the previous process died.
    from .services.processor import resume_pending

    await resume_pending()
    yield


app = FastAPI(
    title="ECS Estimating Agent",
    version="0.1.0",
    description="AI Estimating Agent backend — Phase 0 (foundation)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


app.include_router(auth.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(extraction.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(profile.router, prefix="/api")
app.include_router(trade_relevance.router, prefix="/api")
app.include_router(scope.router, prefix="/api")
app.include_router(bids.router, prefix="/api")
app.include_router(vendors.router, prefix="/api")
app.include_router(vendors.bidleveling_router, prefix="/api")
app.include_router(review.router, prefix="/api")
app.include_router(packages.router, prefix="/api")
app.include_router(audit.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")
app.include_router(outputs.router, prefix="/api")
