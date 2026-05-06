"""Hybrid retrieval: dense + sparse fused with RRF, then Cohere Rerank 3.

Falls back to Claude Haiku as a cross-encoder reranker if no Cohere key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Chunk
from . import bm25_index, embedder

log = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    chunk: Chunk
    dense_score: float
    sparse_score: float
    rrf_score: float
    rerank_score: float | None = None
    snippet: str | None = None


def _rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion of multiple ranked lists."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return fused


async def hybrid_retrieve(
    db: AsyncSession,
    project_id: str,
    query: str,
    top_k: int = 50,
    *,
    filter: dict | None = None,
) -> list[RetrievedChunk]:
    """Run dense + sparse, fuse with RRF, hydrate with chunk rows.

    `filter` is a dict of {column: value} pairs ANDed into the dense lookup
    and post-applied to the sparse results. Used by bid coverage to scope a
    query to a single bid document, and by the discipline agents to scope
    to one CSI section.
    """
    dense_results = await embedder.query_chunks(
        db, project_id, query, top_k=top_k, where=filter
    )
    sparse_results = bm25_index.search(project_id, query, top_k=top_k)
    # Sparse results are unfiltered — apply the same filter post-hoc by
    # joining against the chunks the dense filter would have allowed.
    if filter and sparse_results:
        allowed_ids = {cid for cid, _, _ in dense_results}
        # Also pull any sparse-only IDs that match the filter via SQL
        # (since dense may have missed them).
        from sqlalchemy import select as sql_select
        candidate_ids = [cid for cid, _ in sparse_results]
        if candidate_ids:
            q = sql_select(Chunk.id).where(Chunk.id.in_(candidate_ids))
            for col, val in filter.items():
                if hasattr(Chunk, col):
                    q = q.where(getattr(Chunk, col) == val)
            allowed = (await db.execute(q)).scalars().all()
            allowed_ids = allowed_ids.union(allowed)
        sparse_results = [(cid, s) for cid, s in sparse_results if cid in allowed_ids]

    dense_scores = {cid: s for cid, s, _ in dense_results}
    sparse_scores = {cid: s for cid, s in sparse_results}
    dense_order = [cid for cid, _, _ in dense_results]
    sparse_order = [cid for cid, _ in sparse_results]
    fused = _rrf([dense_order, sparse_order])

    # Top-K by RRF
    ranked_ids = sorted(fused.keys(), key=lambda c: fused[c], reverse=True)[:top_k]
    if not ranked_ids:
        return []

    chunk_rows = await db.execute(select(Chunk).where(Chunk.id.in_(ranked_ids)))
    chunks_by_id = {c.id: c for c in chunk_rows.scalars().all()}

    out: list[RetrievedChunk] = []
    for cid in ranked_ids:
        c = chunks_by_id.get(cid)
        if c is None:
            continue
        out.append(
            RetrievedChunk(
                chunk=c,
                dense_score=dense_scores.get(cid, 0.0),
                sparse_score=sparse_scores.get(cid, 0.0),
                rrf_score=fused[cid],
            )
        )
    return out


# -----------------------------------------------------------------------------
# Reranking
# -----------------------------------------------------------------------------


_cohere_client = None


def _get_cohere():
    global _cohere_client
    if _cohere_client is not None:
        return _cohere_client
    if not settings.cohere_api_key:
        return None
    import cohere

    _cohere_client = cohere.AsyncClientV2(api_key=settings.cohere_api_key)
    return _cohere_client


async def rerank_with_cohere(query: str, candidates: list[RetrievedChunk], top_n: int = 10):
    client = _get_cohere()
    if client is None or not candidates:
        return None
    docs = [c.chunk.text[:8000] for c in candidates]
    try:
        resp = await client.rerank(
            model=settings.rerank_model,
            query=query,
            documents=docs,
            top_n=min(top_n, len(docs)),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("cohere rerank failed: %s — falling back", e)
        return None
    out: list[RetrievedChunk] = []
    for r in resp.results:
        c = candidates[r.index]
        c.rerank_score = float(r.relevance_score)
        out.append(c)
    return out


async def rerank_with_claude(query: str, candidates: list[RetrievedChunk], top_n: int = 10):
    """Fallback cross-encoder using Claude Haiku.

    One call per batch — the model returns relevance scores via tool use for
    every candidate at once. Cheap (cached system prompt) and reliable.
    """
    if not candidates:
        return []
    if not settings.anthropic_api_key:
        # No Anthropic either — degrade to RRF order
        for c in candidates:
            c.rerank_score = c.rrf_score
        return candidates[:top_n]

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    items = [{"id": c.chunk.id, "text": c.chunk.text[:1500]} for c in candidates]

    tool = {
        "name": "score_relevance",
        "description": "Score each candidate's relevance to the query in [0, 1].",
        "input_schema": {
            "type": "object",
            "properties": {
                "scores": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "score": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["id", "score"],
                    },
                }
            },
            "required": ["scores"],
        },
    }

    user = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Score how well each candidate answers the query. "
                    "1.0 = directly answers, 0.0 = irrelevant.\n\n"
                    f"Query: {query}\n\nCandidates:\n"
                    + "\n---\n".join(f"id={x['id']}\n{x['text']}" for x in items)
                ),
            }
        ],
    }

    msg = await client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=4096,
        tools=[tool],
        tool_choice={"type": "tool", "name": "score_relevance"},
        messages=[user],
    )
    score_map: dict[str, float] = {}
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            for s in block.input.get("scores", []):
                score_map[s["id"]] = float(s["score"])
    for c in candidates:
        c.rerank_score = score_map.get(c.chunk.id, c.rrf_score)
    return sorted(candidates, key=lambda c: c.rerank_score, reverse=True)[:top_n]


async def rerank(query: str, candidates: list[RetrievedChunk], top_n: int = 10):
    """Try Cohere; fall back to Claude."""
    ranked = await rerank_with_cohere(query, candidates, top_n=top_n)
    if ranked is not None:
        return ranked
    return await rerank_with_claude(query, candidates, top_n=top_n)


# -----------------------------------------------------------------------------
# Snippet generation
# -----------------------------------------------------------------------------


def make_snippet(chunk: Chunk, query: str, max_len: int = 240) -> str:
    """Return a short snippet from the chunk text, biased toward query terms."""
    text = chunk.text
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    qterms = [t for t in query.lower().split() if len(t) > 1]
    pos = -1
    lower = text.lower()
    for t in qterms:
        p = lower.find(t)
        if p >= 0:
            pos = p
            break
    if pos < 0:
        return text[:max_len].rstrip() + "…"
    start = max(0, pos - 80)
    end = min(len(text), start + max_len)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


async def search(
    db: AsyncSession,
    project_id: str,
    query: str,
    *,
    candidates_k: int = 50,
    final_k: int = 10,
) -> list[RetrievedChunk]:
    """Top-level: hybrid retrieve -> rerank -> attach snippets."""
    candidates = await hybrid_retrieve(db, project_id, query, top_k=candidates_k)
    if not candidates:
        return []
    ranked = await rerank(query, candidates, top_n=final_k)
    for c in ranked:
        c.snippet = make_snippet(c.chunk, query)
    return ranked
