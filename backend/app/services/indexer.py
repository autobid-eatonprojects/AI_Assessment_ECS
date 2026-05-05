"""Orchestrator: turn one document into a fully indexed (Chroma + BM25) state.

Flow per document:
    1. chunker.chunks_for_document  → list[ChunkPayload]
    2. chunker.write_chunks         → SQL rows + contextualized text
    3. embedder.embed_texts         → OpenAI vectors
    4. embedder.upsert_to_collection→ Chroma upsert
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
    delete_from_collection,
    embed_texts,
    upsert_to_collection,
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

        # Wipe any prior chunks for this doc (idempotent)
        existing = await db.execute(select(Chunk).where(Chunk.document_id == document_id))
        old_ids = [c.id for c in existing.scalars().all()]
        if old_ids:
            try:
                delete_from_collection(project_id, old_ids)
            except Exception as e:  # noqa: BLE001
                log.warning("indexer: chroma delete failed: %s", e)

        rows = await write_chunks(db, project_id, document_id, payloads, filename)
        # Snapshot what we need outside the session
        chunk_records = [
            (
                r.id,
                r.contextualized_text or r.text,
                r.text,
                r.document_id,
                r.page_id,
                r.page_number,
                r.chunk_type,
                r.extra or {},
            )
            for r in rows
        ]

    # Embed (separate session)
    try:
        texts = [c[1] for c in chunk_records]
        t0 = time.perf_counter()
        vectors, prompt_tokens = await embed_texts(texts)
        latency_ms = int((time.perf_counter() - t0) * 1000)
    except EmbedderUnavailable:
        log.warning("indexer: OPENAI_API_KEY not set — chunks written but not embedded")
        return {"chunks": len(chunk_records), "embedded": 0, "cost_usd": 0.0, "error": "no openai key"}

    metadatas = [
        {
            "document_id": doc_id,
            "page_id": page_id,
            "page_number": page_number,
            "chunk_type": chunk_type,
            **extra,
        }
        for (_, _, _, doc_id, page_id, page_number, chunk_type, extra) in chunk_records
    ]
    upsert_to_collection(
        project_id=project_id,
        chunk_ids=[c[0] for c in chunk_records],
        embeddings=vectors,
        documents=[c[1] for c in chunk_records],
        metadatas=metadatas,
    )

    # Mark embedded + log cost
    cost_usd = 0.0
    async with SessionLocal() as db:
        usage = Usage(prompt_tokens=prompt_tokens)
        cost, _ = await record_call(
            db,
            purpose="embed",
            model=f"cohere:{settings.embedding_model}",
            usage=usage,
            latency_ms=latency_ms,
            project_id=project_id,
            document_id=document_id,
            provider="cohere",
        )
        cost_usd = cost or 0.0
        await db.execute(
            update(Chunk).where(Chunk.id.in_([c[0] for c in chunk_records])).values(embedded=True)
        )
        await db.commit()

    # Rebuild BM25 over ALL project chunks (cheap)
    async with SessionLocal() as db:
        all_chunks = await db.execute(
            select(Chunk).where(Chunk.project_id == project_id).order_by(Chunk.created_at)
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
        len(chunk_records),
        prompt_tokens,
        latency_ms,
        cost_usd,
    )
    return {
        "chunks": len(chunk_records),
        "embedded": len(chunk_records),
        "tokens": prompt_tokens,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
    }
