"""Per-project BM25 sparse index, persisted to JSON.

Catches exact codes / identifiers that dense embeddings can miss
(e.g. code references like "NFPA 13" / "ASTM E1264", manufacturer model
numbers, sheet IDs like "S1.1", schedule mark IDs like "F6.0").
Tokenisation is plain lower-case alphanumeric splits — good enough for
engineering callouts.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

from ..config import settings

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _index_dir() -> Path:
    p = (settings.storage_root / ".." / "bm25").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _index_file(project_id: str) -> Path:
    return _index_dir() / f"{project_id}.json"


def save_index(project_id: str, chunk_ids: list[str], texts: list[str]) -> None:
    payload = {
        "ids": chunk_ids,
        "tokens": [_tokenize(t) for t in texts],
    }
    _index_file(project_id).write_text(json.dumps(payload))
    log.info("bm25: wrote %d chunks for project %s", len(chunk_ids), project_id)


def load_index(project_id: str) -> tuple[list[str], BM25Okapi] | None:
    path = _index_file(project_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    ids = payload.get("ids") or []
    tokens = payload.get("tokens") or []
    if not ids:
        return None
    return ids, BM25Okapi(tokens)


def search(project_id: str, query: str, top_k: int = 50) -> list[tuple[str, float]]:
    """Return [(chunk_id, score)] sorted best-first."""
    loaded = load_index(project_id)
    if loaded is None:
        return []
    ids, bm25 = loaded
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    scores = bm25.get_scores(q_tokens)
    pairs = list(zip(ids, scores, strict=True))
    pairs.sort(key=lambda p: p[1], reverse=True)
    return [(cid, float(s)) for cid, s in pairs[:top_k] if s > 0]
