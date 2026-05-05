"""Cohere Embed v4 -> Chroma persistent store.

One Chroma collection per project (`project_<id>`). Vector ids match
`Chunk.id` so Chroma is just a fast nearest-neighbour cache; the canonical
chunk text + metadata lives in SQLite.

We use Cohere Embed v4 (multimodal, 128K context, Matryoshka 256/512/1024/1536)
because:
- Same vendor as our reranker (Cohere Rerank 3) — single key + billing
- Multimodal: lets us upgrade to image-aware retrieval in a future phase
  without re-embedding the corpus
- Cheap: ~$0.12 / 1M tokens
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import chromadb

from ..config import settings

log = logging.getLogger(__name__)


class EmbedderUnavailable(Exception):
    """Raised when no Cohere API key is configured."""


_cohere_client = None
_chroma_client: chromadb.PersistentClient | None = None
_concurrency_sem: asyncio.Semaphore | None = None


def _get_cohere():
    global _cohere_client
    if _cohere_client is not None:
        return _cohere_client
    if not settings.cohere_api_key:
        raise EmbedderUnavailable("COHERE_API_KEY is not set")
    import cohere

    _cohere_client = cohere.AsyncClientV2(api_key=settings.cohere_api_key)
    return _cohere_client


def _get_chroma() -> chromadb.PersistentClient:
    global _chroma_client
    if _chroma_client is None:
        path = settings.storage_root / ".." / "chroma"
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(path))
    return _chroma_client


def _get_semaphore() -> asyncio.Semaphore:
    global _concurrency_sem
    if _concurrency_sem is None:
        _concurrency_sem = asyncio.Semaphore(settings.index_concurrency)
    return _concurrency_sem


def _collection_name(project_id: str) -> str:
    return f"project_{project_id.replace('-', '')}"


def get_collection(project_id: str):
    return _get_chroma().get_or_create_collection(name=_collection_name(project_id))


def reset_collection(project_id: str) -> None:
    """Drop and recreate the collection — used when re-indexing a project."""
    client = _get_chroma()
    try:
        client.delete_collection(_collection_name(project_id))
    except Exception:  # noqa: BLE001
        pass
    client.get_or_create_collection(name=_collection_name(project_id))


# -----------------------------------------------------------------------------
# Embedding
# -----------------------------------------------------------------------------


async def _embed_batch(
    texts: list[str], input_type: str = "search_document"
) -> tuple[list[list[float]], dict[str, int]]:
    """Embed a batch with Cohere Embed v4. Returns (vectors, usage).

    Cohere recommends `input_type='search_document'` for indexing and
    `'search_query'` for query-time embeds — we honour that.
    """
    client = _get_cohere()
    sem = _get_semaphore()
    async with sem:
        resp = await client.embed(
            model=settings.embedding_model,
            texts=texts,
            input_type=input_type,
            embedding_types=["float"],
            output_dimension=settings.embedding_dim,
        )
    vectors = list(resp.embeddings.float)
    # Cohere returns billed_units.input_tokens
    billed = getattr(resp.meta, "billed_units", None)
    prompt_tokens = getattr(billed, "input_tokens", 0) if billed else 0
    return vectors, {"prompt_tokens": prompt_tokens or 0, "completion_tokens": 0}


async def embed_texts(texts: list[str], batch_size: int = 96) -> tuple[list[list[float]], int]:
    """Embed many texts; returns (vectors, total_prompt_tokens)."""
    vectors: list[list[float]] = []
    total_tokens = 0
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        vecs, usage = await _embed_batch(batch)
        vectors.extend(vecs)
        total_tokens += usage["prompt_tokens"]
    return vectors, total_tokens


def upsert_to_collection(
    project_id: str,
    chunk_ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    """Idempotent upsert into the project's Chroma collection."""
    if not chunk_ids:
        return
    coll = get_collection(project_id)
    # Chroma's metadata must be flat (str/int/float/bool). Sanitize.
    sanitized: list[dict[str, Any]] = []
    for m in metadatas:
        flat: dict[str, Any] = {}
        for k, v in (m or {}).items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                flat[k] = v
            else:
                flat[k] = str(v)
        sanitized.append(flat)
    coll.upsert(
        ids=chunk_ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=sanitized,
    )


def delete_from_collection(project_id: str, chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    coll = get_collection(project_id)
    coll.delete(ids=chunk_ids)


async def query_collection(
    project_id: str,
    query_text: str,
    top_k: int = 50,
    where: dict | None = None,
) -> list[tuple[str, float, dict]]:
    """Vector-search the project collection.

    Returns list of (chunk_id, similarity, metadata) sorted best-first.
    Chroma returns L2 distance by default; we expose 1/(1+distance) as a
    similarity for fusion.
    """
    coll = get_collection(project_id)
    qvec, _ = await _embed_batch([query_text], input_type="search_query")
    res = coll.query(
        query_embeddings=qvec,
        n_results=top_k,
        where=where,
    )
    ids = (res.get("ids") or [[]])[0]
    distances = (res.get("distances") or [[]])[0]
    metadatas = (res.get("metadatas") or [[]])[0]
    out: list[tuple[str, float, dict]] = []
    for i, id_ in enumerate(ids):
        d = distances[i] if i < len(distances) else None
        sim = 1.0 / (1.0 + d) if d is not None else 0.0
        out.append((id_, sim, metadatas[i] if i < len(metadatas) else {}))
    return out
