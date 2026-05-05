"""LLM call accounting: cost computation + persistent audit log.

Every call to Anthropic (classification, vision pre-pass, future agents) flows
through here so we can show "$X spent on this project" in the UI and audit
exactly what was sent / received for any given page on a $20M decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import LLMCall

log = logging.getLogger(__name__)


# Anthropic per-million-token pricing (USD), keyed by model id.
# Update when pricing changes; values used only for cost display.
PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    # OpenAI embeddings — input only, no output cost
    "openai:text-embedding-3-large": {"input": 0.13, "output": 0.0},
    "openai:text-embedding-3-small": {"input": 0.02, "output": 0.0},
    # Cohere — embed v4 (input) + rerank v3 (per search, ~$0.001)
    "cohere:embed-v4.0": {"input": 0.12, "output": 0.0},
    "cohere:rerank-v3.5": {"input": 0.0, "output": 0.0},  # billed per-search
}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


def compute_cost(model: str, usage: Usage) -> float | None:
    """Best-effort USD cost. Returns None for unknown models."""
    # Strip any timestamp-style suffix (e.g. "claude-haiku-4-5-20251001")
    base = "-".join(model.split("-")[:4])
    rates = PRICING_USD_PER_MTOK.get(base) or PRICING_USD_PER_MTOK.get(model)
    if rates is None:
        return None
    return (
        (usage.prompt_tokens / 1_000_000) * rates["input"]
        + (usage.completion_tokens / 1_000_000) * rates["output"]
        + (usage.cache_write_tokens / 1_000_000) * rates.get("cache_write", rates["input"])
        + (usage.cache_read_tokens / 1_000_000) * rates.get("cache_read", rates["input"])
    )


def usage_from_anthropic(message) -> Usage:
    """Read the .usage block off an Anthropic SDK Message object."""
    u = getattr(message, "usage", None)
    if u is None:
        return Usage()
    return Usage(
        prompt_tokens=getattr(u, "input_tokens", 0) or 0,
        completion_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
    )


async def record_call(
    db: AsyncSession,
    *,
    purpose: str,
    model: str,
    usage: Usage,
    latency_ms: int,
    status: str = "ok",
    error: str | None = None,
    project_id: str | None = None,
    document_id: str | None = None,
    page_extraction_id: str | None = None,
    provider: str = "anthropic",
) -> tuple[float | None, LLMCall]:
    """Persist a row in llm_calls and return (cost_usd, row)."""
    cost = compute_cost(model, usage)
    row = LLMCall(
        project_id=project_id,
        document_id=document_id,
        page_extraction_id=page_extraction_id,
        purpose=purpose,
        model=model,
        provider=provider,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        status=status,
        error=error,
    )
    db.add(row)
    await db.flush()
    return cost, row
