"""Phase 4.3d — Dedupe scope candidates by (csi_code + description similarity).

After the multi-query EVE extractor, the same scope item may appear up to 3
times — once per query perspective. Deduping by description similarity (via
Cohere Embed v4 cosine) collapses them and merges their citations.

We keep the highest-confidence variant as canonical and union the supporting
chunks across duplicates so the UI shows every source that mentioned the
item.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np

from .embedder import EmbedderUnavailable, embed_texts
from .scope_extractor import CandidateItem

log = logging.getLogger(__name__)


_SIMILARITY_THRESHOLD = 0.92


async def dedupe(
    items: list[CandidateItem],
) -> list[CandidateItem]:
    """Cluster candidates by (csi_code + description similarity ≥ 0.92).

    Returns one merged candidate per cluster, with all supporting chunks
    accumulated. Falls back to no-op dedupe if the embedder isn't available.
    """
    if not items:
        return []

    try:
        vectors, _ = await embed_texts([it.description for it in items], batch_size=96)
    except EmbedderUnavailable:
        log.warning("scope_deduper: embedder unavailable; skipping dedupe")
        return items

    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.where(norms == 0, 1, norms)  # row-normalise

    n = len(items)
    cluster_ids: list[int] = [-1] * n
    next_cluster = 0

    for i in range(n):
        if cluster_ids[i] != -1:
            continue
        cluster_ids[i] = next_cluster
        for j in range(i + 1, n):
            if cluster_ids[j] != -1:
                continue
            # Same CSI code (we already grounded) AND embedding cosine ≥ threshold
            if items[i].csi_code != items[j].csi_code:
                continue
            sim = float(arr[i] @ arr[j])
            if sim >= _SIMILARITY_THRESHOLD:
                cluster_ids[j] = next_cluster
        next_cluster += 1

    # Merge clusters
    merged: list[CandidateItem] = []
    for c_id in range(next_cluster):
        members = [items[i] for i in range(n) if cluster_ids[i] == c_id]
        merged.append(_merge_cluster(members))

    log.info(
        "scope_deduper: %d candidates → %d after dedupe (%.0f%% reduction)",
        n,
        len(merged),
        (1 - len(merged) / n) * 100 if n else 0,
    )
    return merged


def _merge_cluster(members: list[CandidateItem]) -> CandidateItem:
    """Pick the longest-description variant as canonical; union citations."""
    if len(members) == 1:
        return members[0]

    # Pick the one with the most complete/longest description.
    canonical = max(
        members, key=lambda c: (len(c.description or ""), c.specification or "")
    )

    # Union supporting chunks (by chunk_id) and source_chunk_ids
    seen_chunk_ids: set[str] = set()
    merged_chunks = []
    for m in members:
        for r in m.supporting_chunks:
            if r.chunk.id not in seen_chunk_ids:
                seen_chunk_ids.add(r.chunk.id)
                merged_chunks.append(r)
    union_source_ids = sorted(seen_chunk_ids)

    # Fill nulls from other members where canonical lacks them.
    return replace(
        canonical,
        specification=canonical.specification or _first_nonnull(members, "specification"),
        quantity=canonical.quantity or _first_nonnull(members, "quantity"),
        unit=canonical.unit or _first_nonnull(members, "unit"),
        location=canonical.location or _first_nonnull(members, "location"),
        source_chunk_ids=union_source_ids,
        supporting_chunks=merged_chunks,
        # Provenance: combine query tags so we know which queries surfaced it
        found_by_query="+".join(sorted({m.found_by_query for m in members if m.found_by_query})),
    )


def _first_nonnull(members, attr):
    for m in members:
        v = getattr(m, attr)
        if v:
            return v
    return None
