"""Orchestrator: turn one document into a fully indexed (pgvector + BM25) state.

Flow per document:
    1. chunker.chunks_for_document  → list[ChunkPayload]
    2. chunker.write_chunks         → SQL rows + contextualized text
    3. embedder.embed_texts         → voyage-3-large vectors (1024d)
    4. embedder.upsert_embeddings   → UPDATE chunks SET embedding = ...
    5. log LLMCall                  → cost + latency audit
    6. bm25_index.save_index        → rebuild project's BM25 over ALL chunks

Step 6 is project-wide because BM25 needs the whole corpus to compute IDF; it
gets rebuilt every time any document in the project finishes indexing. Cheap
even at thousands of chunks (it's a JSON tokenisation).
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select, update

from ..config import settings
from ..database import SessionLocal
from ..models import Chunk, Document
from .bm25_index import save_index
from .chunker import chunks_for_document, write_chunks
from .embedder import (
    EmbedderUnavailable,
    embed_texts,
    upsert_embeddings,
)
from .llm_log import Usage, record_call

log = logging.getLogger(__name__)


async def index_document(document_id: str) -> dict:
    """Index one document. Returns {chunks, embedded, cost_usd, error?}."""
    async with SessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            return {"error": "doc not found"}
        project_id = doc.project_id
        filename = doc.filename

        log.info("indexer: starting %s (%s)", filename, document_id)
        payloads = await chunks_for_document(db, doc)
        if not payloads:
            log.info("indexer: no chunks for %s — nothing to index", filename)
            return {"chunks": 0, "embedded": 0, "cost_usd": 0.0}

        # Wipe any prior chunks for this doc (idempotent re-index). With
        # pgvector the embeddings live ON the chunk row, so deleting the
        # row removes both the metadata AND the vector — no second-store
        # cleanup needed.
        await db.execute(
            Chunk.__table__.delete().where(Chunk.document_id == document_id)
        )
        await db.commit()

        rows = await write_chunks(db, project_id, document_id, payloads, filename)
        # Snapshot what we need outside the session (we need IDs + the text
        # to embed)
        chunk_records = [
            (r.id, r.contextualized_text or r.text)
            for r in rows
        ]

    # Embed (separate session)
    try:
        texts = [c[1] for c in chunk_records]
        t0 = time.perf_counter()
        vectors, prompt_tokens = await embed_texts(texts)
        latency_ms = int((time.perf_counter() - t0) * 1000)
    except EmbedderUnavailable:
        log.warning("indexer: VOYAGE_API_KEY not set — chunks written but not embedded")
        return {
            "chunks": len(chunk_records),
            "embedded": 0,
            "cost_usd": 0.0,
            "error": "no voyage key",
        }

    # Persist vectors directly onto chunk rows + log the cost
    cost_usd = 0.0
    chunk_id_to_vec = dict(zip([c[0] for c in chunk_records], vectors))
    async with SessionLocal() as db:
        embedded_count = await upsert_embeddings(db, chunk_id_to_vec)
        usage = Usage(prompt_tokens=prompt_tokens)
        cost, _ = await record_call(
            db,
            purpose="embed",
            model=f"voyage:{settings.embedding_model}",
            usage=usage,
            latency_ms=latency_ms,
            project_id=project_id,
            document_id=document_id,
            provider="voyage",
        )
        cost_usd = cost or 0.0
        await db.commit()

    # Rebuild BM25 over ALL project chunks (cheap)
    async with SessionLocal() as db:
        all_chunks = await db.execute(
            select(Chunk)
            .where(Chunk.project_id == project_id)
            .order_by(Chunk.created_at)
        )
        chunks = list(all_chunks.scalars().all())
    save_index(
        project_id=project_id,
        chunk_ids=[c.id for c in chunks],
        texts=[c.contextualized_text or c.text for c in chunks],
    )

    log.info(
        "indexer: %s indexed — %d chunks, %d tokens, %dms, $%.4f",
        filename,
        embedded_count,
        prompt_tokens,
        latency_ms,
        cost_usd,
    )
    return {
        "chunks": len(chunk_records),
        "embedded": embedded_count,
        "tokens": prompt_tokens,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
    }
