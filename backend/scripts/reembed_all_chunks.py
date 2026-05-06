"""Re-embed every chunk in Postgres via voyage-3-large → pgvector.

Use cases:
  - After SQLite → Postgres migration (chunks land with embedding=NULL)
  - When swapping embedding models (e.g. Cohere → voyage)
  - When the embedding column dim changes

Usage:
    .venv/bin/python -m scripts.reembed_all_chunks
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Chunk, Project
from app.services.embedder import (
    EmbedderUnavailable,
    embed_texts,
    upsert_embeddings,
)
from app.services.bm25_index import save_index
from app.services.llm_log import Usage, record_call
from app.config import settings

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


BATCH_SIZE = 96


async def reembed_project(project_id: str) -> dict:
    """Re-embed every chunk for one project. Returns counts + cost."""
    async with SessionLocal() as db:
        chunks = (
            await db.execute(
                select(Chunk)
                .where(Chunk.project_id == project_id)
                .order_by(Chunk.created_at)
            )
        ).scalars().all()
    if not chunks:
        log.info("project %s: no chunks", project_id)
        return {"chunks": 0, "embedded": 0, "cost_usd": 0.0}

    # Batch + embed
    texts = [c.contextualized_text or c.text for c in chunks]
    log.info("project %s: embedding %d chunks", project_id, len(chunks))
    t0 = time.perf_counter()
    try:
        vectors, prompt_tokens = await embed_texts(texts, batch_size=BATCH_SIZE)
    except EmbedderUnavailable as e:
        log.error("project %s: %s", project_id, e)
        return {"error": str(e)}
    latency_ms = int((time.perf_counter() - t0) * 1000)

    # Persist into pgvector + log cost
    chunk_id_to_vec = dict(zip([c.id for c in chunks], vectors))
    async with SessionLocal() as db:
        embedded = await upsert_embeddings(db, chunk_id_to_vec)
        cost, _ = await record_call(
            db,
            purpose="embed-reembed",
            model=f"voyage:{settings.embedding_model}",
            usage=Usage(prompt_tokens=prompt_tokens),
            latency_ms=latency_ms,
            project_id=project_id,
            provider="voyage",
        )
        await db.commit()

    # Rebuild BM25 (reads from chunk text, doesn't need embeddings)
    save_index(
        project_id=project_id,
        chunk_ids=[c.id for c in chunks],
        texts=[c.contextualized_text or c.text for c in chunks],
    )
    log.info(
        "project %s: %d chunks embedded in %dms, $%.4f",
        project_id, embedded, latency_ms, cost or 0.0,
    )
    return {
        "chunks": len(chunks),
        "embedded": embedded,
        "tokens": prompt_tokens,
        "cost_usd": cost or 0.0,
        "latency_ms": latency_ms,
    }


async def main() -> None:
    async with SessionLocal() as db:
        projects = (await db.execute(select(Project))).scalars().all()
    total_cost = 0.0
    total_chunks = 0
    for p in projects:
        result = await reembed_project(p.id)
        if "cost_usd" in result:
            total_cost += result["cost_usd"]
            total_chunks += result.get("embedded", 0)
    log.info(
        "ALL DONE — %d projects, %d chunks, $%.4f total",
        len(projects), total_chunks, total_cost,
    )


if __name__ == "__main__":
    asyncio.run(main())
