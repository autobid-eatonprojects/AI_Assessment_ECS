"""Phase 11 — vendor name canonicalization.

After classification, every bid_submission Document carries a `vendor_name`
either typed by the operator or extracted from the letterhead by the
classifier. The same legal entity often shows up under multiple spellings:

  TLC
  Tennessee Lawn Care, Inc.
  Tennessee Lawn Care
  The Cleaning Leaders LLC

Without canonicalization the vendor profile view shows four "vendors"
instead of one. This pass groups near-duplicates by Cohere embedding
similarity and writes the chosen canonical form back to
`Document.canonical_vendor` so the profile aggregation can `GROUP BY`
cleanly.

Approach: pure embedding clustering, no LLM call. Cohere Embed v4 in
classification mode → cosine similarity ≥ 0.78 (slightly looser than
scope dedupe's 0.92 because vendor names are short and lossy). Cluster
by union-find. Pick the longest variant per cluster as canonical (the
fully-spelled legal name is more useful than the abbreviation).

Cost: one Cohere embed batch per project (~$0.001). Latency: <2s.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, update

from ..database import SessionLocal
from ..models import Document
from .embedder import EmbedderUnavailable, embed_texts

log = logging.getLogger(__name__)


_SIMILARITY_THRESHOLD = 0.78


@dataclass
class _UnionFind:
    parent: dict[int, int]

    @classmethod
    def of(cls, n: int) -> "_UnionFind":
        return cls({i: i for i in range(n)})

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _normalize(name: str) -> str:
    """Light normalization for embedding — strip punctuation/legal suffixes
    so 'TLC, Inc.' and 'TLC' embed close together."""
    s = name.strip().lower()
    for suffix in [
        ", inc.",
        " inc.",
        ", inc",
        " inc",
        ", llc",
        " llc",
        ", ltd",
        " ltd",
        ", co.",
        " co.",
        " corp.",
        " corp",
    ]:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip(", ")
    # Collapse whitespace
    return " ".join(s.split())


def _pick_canonical(variants: list[str]) -> str:
    """Among aliases for one entity, choose the most informative form.

    Heuristics, in priority:
      1. Longest non-acronym name (an acronym is all-caps + ≤4 chars)
      2. Falls back to longest by length
    """
    non_acronym = [
        v for v in variants if not (v.isupper() and len(v.replace(" ", "")) <= 4)
    ]
    pool = non_acronym or variants
    return max(pool, key=lambda v: (len(v), v))


async def canonicalize_vendors(project_id: str) -> dict[str, str]:
    """Cluster all vendor_name variants seen on this project's bid documents
    and write the canonical form to Document.canonical_vendor.

    Returns a mapping {original_vendor_name: canonical_vendor_name}.
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Document.id, Document.vendor_name)
                .where(Document.project_id == project_id)
                .where(Document.source == "bid_submission")
                .where(Document.vendor_name.is_not(None))
            )
        ).all()

    if not rows:
        log.info("vendor_canonicalizer: no bid documents for project %s", project_id)
        return {}

    # Unique vendor_name strings (case-insensitive)
    by_lower: dict[str, str] = {}
    for _id, name in rows:
        if name and name.strip():
            key = name.strip().lower()
            # Keep the first form we see as the representative for this lowercase key
            by_lower.setdefault(key, name.strip())

    unique_names = list(by_lower.values())
    if len(unique_names) <= 1:
        canonical = unique_names[0] if unique_names else None
        if canonical:
            async with SessionLocal() as db:
                await db.execute(
                    update(Document)
                    .where(Document.project_id == project_id)
                    .where(Document.source == "bid_submission")
                    .where(Document.vendor_name.is_not(None))
                    .values(canonical_vendor=canonical)
                )
                await db.commit()
            return {canonical: canonical}
        return {}

    # Embed normalized forms; cosine ≥ threshold → same vendor
    normalized = [_normalize(n) for n in unique_names]
    try:
        vectors, _ = await embed_texts(normalized, batch_size=96)
    except EmbedderUnavailable:
        log.warning("vendor_canonicalizer: no embedder; using exact-match clustering")
        # Each unique name becomes its own canonical
        mapping = {n: n for n in unique_names}
        await _write_canonical(project_id, mapping)
        return mapping

    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.where(norms == 0, 1, norms)

    n = len(unique_names)
    uf = _UnionFind.of(n)
    for i in range(n):
        for j in range(i + 1, n):
            if float(arr[i] @ arr[j]) >= _SIMILARITY_THRESHOLD:
                uf.union(i, j)

    # Group by cluster root → canonical name
    clusters: dict[int, list[str]] = defaultdict(list)
    for i in range(n):
        clusters[uf.find(i)].append(unique_names[i])

    mapping: dict[str, str] = {}
    for members in clusters.values():
        canonical = _pick_canonical(members)
        for m in members:
            mapping[m] = canonical

    log.info(
        "vendor_canonicalizer: %d unique vendors → %d canonical clusters for project %s",
        n,
        len(clusters),
        project_id,
    )

    await _write_canonical(project_id, mapping)
    return mapping


async def _write_canonical(
    project_id: str, mapping: dict[str, str]
) -> None:
    """Write canonical_vendor to every bid Document based on its current
    vendor_name. Uses a per-vendor UPDATE rather than per-row to minimize
    round-trips."""
    if not mapping:
        return
    async with SessionLocal() as db:
        for original, canonical in mapping.items():
            await db.execute(
                update(Document)
                .where(Document.project_id == project_id)
                .where(Document.source == "bid_submission")
                .where(Document.vendor_name == original)
                .values(canonical_vendor=canonical)
            )
        await db.commit()
