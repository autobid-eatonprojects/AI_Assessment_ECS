"""voyage-3-large -> pgvector single-store embedder.

Embeddings live on `chunks.embedding` (pgvector `Vector(1024)`) so retrieval
is one SQL query (`ORDER BY embedding <=> $vec LIMIT k`) with ACID joins
to scope items, citations, packages, etc. — no second-store hop.

Why voyage-3-large:
  - Highest published retrieval scores on technical / engineering text
    (the design doc cites it specifically over Cohere Embed v4)
  - 1024-dim output matches the pgvector column dimension
  - Async client, batches up to ~128 inputs

Why pgvector:
  - Single source of truth: relational rows + vectors in one DB
  - HNSW index gives sub-millisecond ANN on hundreds of thousands of vectors
  - Joins to chunk metadata / source documents / scope items are FREE (no
    ID round-trip through a second store)
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Chunk

log = logging.getLogger(__name__)


class EmbedderUnavailable(Exception):
    """Raised when no Voyage API key is configured."""


_voyage_client = None
_concurrency_sem: asyncio.Semaphore | None = None


def _get_voyage():
    global _voyage_client
    if _voyage_client is not None:
        return _voyage_client
    if not settings.voyage_api_key:
        raise EmbedderUnavailable("VOYAGE_API_KEY is not set")
    import voyageai

    _voyage_client = voyageai.AsyncClient(api_key=settings.voyage_api_key)
    return _voyage_client


def _get_semaphore() -> asyncio.Semaphore:
    global _concurrency_sem
    if _concurrency_sem is None:
        _concurrency_sem = asyncio.Semaphore(settings.index_concurrency)
    return _concurrency_sem


# -----------------------------------------------------------------------------
# Embedding API
# -----------------------------------------------------------------------------


async def _embed_batch(
    texts: list[str], input_type: str = "document"
) -> tuple[list[list[float]], dict[str, int]]:
    """Embed a batch with voyage-3-large. Returns (vectors, usage).

    Voyage uses ``input_type='document'`` for indexing and
    ``'query'`` for query-time embeds — we honour that for max recall.
    """
    client = _get_voyage()
    sem = _get_semaphore()
    async with sem:
        resp = await client.embed(
            texts=texts,
            model=settings.embedding_model,
            input_type=input_type,
            output_dimension=settings.embedding_dim,
        )
    vectors = list(resp.embeddings)
    total_tokens = getattr(resp, "total_tokens", 0) or 0
    return vectors, {"prompt_tokens": total_tokens, "completion_tokens": 0}


async def embed_texts(
    texts: list[str], batch_size: int = 96
) -> tuple[list[list[float]], int]:
    """Embed many texts; returns (vectors, total_prompt_tokens)."""
    vectors: list[list[float]] = []
    total_tokens = 0
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        vecs, usage = await _embed_batch(batch)
        vectors.extend(vecs)
        total_tokens += usage["prompt_tokens"]
    return vectors, total_tokens


# -----------------------------------------------------------------------------
# Persistence to pgvector
# -----------------------------------------------------------------------------


async def upsert_embeddings(
    db: AsyncSession,
    chunk_id_to_vector: dict[str, list[float]],
) -> int:
    """Set `embedding` on a batch of Chunk rows; mark `embedded=True`.

    No "collection" abstraction — pgvector is just a column on the chunks
    table, partitioned naturally by `project_id` via WHERE clauses.
    """
    if not chunk_id_to_vector:
        return 0
    n = 0
    for chunk_id, vec in chunk_id_to_vector.items():
        await db.execute(
            update(Chunk)
            .where(Chunk.id == chunk_id)
            .values(embedding=vec, embedded=True)
        )
        n += 1
    await db.commit()
    return n


async def clear_embeddings_for_project(db: AsyncSession, project_id: str) -> int:
    """Reset every chunk's embedding for a project — used on full re-index."""
    result = await db.execute(
        update(Chunk)
        .where(Chunk.project_id == project_id)
        .values(embedding=None, embedded=False)
    )
    await db.commit()
    return result.rowcount or 0


# -----------------------------------------------------------------------------
# Query (vector search)
# -----------------------------------------------------------------------------


async def query_chunks(
    db: AsyncSession,
    project_id: str,
    query_text: str,
    top_k: int = 50,
    where: dict | None = None,
) -> list[tuple[str, float, dict]]:
    """Vector-search the project's chunks via pgvector cosine distance.

    Returns list of (chunk_id, similarity_score, metadata) sorted best-first.

    pgvector's ``<=>`` operator returns cosine *distance* (0=identical,
    2=opposite). We expose ``1 - distance`` as a similarity for fusion with
    BM25 scores.

    `where` is a dict of {column: value} pairs ANDed into the SQL filter —
    used by bid coverage to scope a query to a single bid document, etc.
    """
    qvec, _ = await _embed_batch([query_text], input_type="query")
    qvec_str = "[" + ",".join(str(x) for x in qvec[0]) + "]"

    from sqlalchemy import text as sql_text

    extra_filters = ""
    params: dict[str, object] = {
        "project_id": project_id,
        "qvec": qvec_str,
        "top_k": top_k,
    }
    if where:
        # Whitelist: only allow filtering on columns we know exist on chunks.
        # Prevents accidental SQL injection via unexpected keys.
        allowed = {"document_id", "chunk_type", "page_number", "source_id"}
        for i, (col, val) in enumerate(where.items()):
            if col not in allowed:
                continue
            param_name = f"w{i}"
            extra_filters += f" AND {col} = :{param_name}"
            params[param_name] = val

    sql = sql_text(
        f"""
        SELECT id, extra,
               1 - (embedding <=> CAST(:qvec AS vector)) AS sim
        FROM chunks
        WHERE project_id = :project_id
          AND embedding IS NOT NULL
          {extra_filters}
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
        """
    )
    rows = (await db.execute(sql, params)).all()
    return [(row[0], float(row[2]), row[1] or {}) for row in rows]
