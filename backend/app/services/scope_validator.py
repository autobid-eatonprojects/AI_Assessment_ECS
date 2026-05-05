"""Phase 4.3b — 3-vote majority validator (EVE Stage B / L3X scrutinize).

For each candidate scope item from Stage A, ask Claude Haiku 4.5 three times
"does the supplied source genuinely support this item?". Keep the candidate
only if at least 2 of 3 say yes.

Three independent calls (rather than one with N=3 in the prompt) is what
the research literature advocates: independent samples reduce correlated
errors better than a single judge. Each call is cheap (~$0.0005), so the
3x cost is trivial vs the precision gain.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..config import settings
from .llm_log import Usage, record_call, usage_from_anthropic
from .retriever import RetrievedChunk
from .scope_extractor import CandidateItem

log = logging.getLogger(__name__)


_VALIDATE_TOOL = {
    "name": "judge_scope_item",
    "description": (
        "Decide whether the supplied evidence chunks genuinely support the "
        "claimed scope item. The item should be ACCEPTED only if a reasonable "
        "estimator could read the chunks and conclude the item is in scope; "
        "REJECTED if the chunks don't actually mention or imply it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "accept": {"type": "boolean"},
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence explaining the verdict.",
            },
        },
        "required": ["accept", "confidence", "reasoning"],
    },
}


@dataclass
class ValidationResult:
    accept: bool
    confidence: float  # average across the 3 votes (yes=conf, no=1-conf)
    votes: list[dict]  # raw vote payloads for audit


_validator_client = None


def _get_client():
    global _validator_client
    if _validator_client is not None:
        return _validator_client
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    _validator_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _validator_client


def _format_chunks(chunks: list[RetrievedChunk]) -> str:
    parts = []
    for r in chunks[:6]:  # Cap to keep prompts cheap
        c = r.chunk
        text = c.text if len(c.text) <= 800 else c.text[:800] + "…"
        parts.append(f"[chunk {c.id} | type={c.chunk_type}]\n{text}")
    return "\n\n---\n\n".join(parts)


def _build_validate_prompt(item: CandidateItem, framing: str) -> str:
    chunks = _format_chunks(item.supporting_chunks)
    return (
        f"{framing}\n\n"
        f"=== CLAIMED SCOPE ITEM ===\n"
        f"CSI code: {item.csi_code}\n"
        f"Description: {item.description}\n"
        f"Specification: {item.specification or '(none)'}\n"
        f"Quantity: {item.quantity or '(none)'} {item.unit or ''}\n"
        f"Location: {item.location or '(none)'}\n"
        f"Extraction method: {item.extraction_method}\n\n"
        f"=== EVIDENCE CHUNKS ===\n{chunks}\n\n"
        f"=== TASK ===\n"
        f"Use judge_scope_item to ACCEPT (chunks support the item) or "
        f"REJECT (chunks don't actually support it)."
    )


# Three different framings of the same yes/no question — improves
# independence of the votes per the EVE / L3X majority-vote pattern.
_FRAMINGS = [
    "You are a senior estimator double-checking that an extracted scope "
    "item is genuinely supported by the source documents. Apply normal "
    "professional judgement.",
    "You are an auditor verifying that an estimating assistant didn't "
    "hallucinate. Be strict — accept only if the chunks clearly mention "
    "or directly imply the claimed item.",
    "You are a project manager spot-checking a scope of work for legal "
    "defensibility. Accept the item only if a reasonable reader of the "
    "chunks would agree it's part of the project's scope.",
]


async def _vote_once(
    client, item: CandidateItem, framing: str, project_id: str
) -> tuple[dict, Usage, int]:
    t0 = time.perf_counter()
    msg = await client.messages.create(
        model=settings.classifier_model,  # Haiku 4.5
        max_tokens=512,
        tools=[_VALIDATE_TOOL],
        tool_choice={"type": "tool", "name": "judge_scope_item"},
        messages=[
            {"role": "user", "content": _build_validate_prompt(item, framing)}
        ],
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "judge_scope_item"
        ):
            return block.input, usage_from_anthropic(msg), latency_ms
    raise RuntimeError("validator: no tool_use block in Haiku response")


async def validate_candidate(
    item: CandidateItem,
    project_id: str,
    sem: asyncio.Semaphore,
) -> tuple[ValidationResult, float]:
    """Run 3 independent validators on the candidate. Returns (result, cost)."""
    client = _get_client()
    if not item.supporting_chunks:
        # No evidence at all — auto-reject
        return (
            ValidationResult(accept=False, confidence=0.0, votes=[]),
            0.0,
        )

    async def one(framing: str):
        async with sem:
            try:
                return await _vote_once(client, item, framing, project_id)
            except Exception as e:  # noqa: BLE001
                log.warning("validator: vote failed: %s", e)
                return None

    results = await asyncio.gather(*(one(f) for f in _FRAMINGS))
    successful = [r for r in results if r is not None]
    if not successful:
        return (
            ValidationResult(accept=False, confidence=0.0, votes=[]),
            0.0,
        )

    votes = [r[0] for r in successful]
    accepts = sum(1 for v in votes if v.get("accept"))

    # Confidence: yes-votes contribute their stated confidence, no-votes
    # contribute (1 - stated). Average across all votes.
    conf_sum = 0.0
    for v in votes:
        c = float(v.get("confidence", 0.5) or 0.5)
        conf_sum += c if v.get("accept") else (1.0 - c)
    avg_conf = conf_sum / len(votes)

    # Cost accounting (3 calls)
    from ..database import SessionLocal

    total_cost = 0.0
    async with SessionLocal() as db:
        for r in successful:
            _, usage, latency_ms = r
            cost, _ = await record_call(
                db,
                purpose="scope-validate",
                model=settings.classifier_model,
                usage=usage,
                latency_ms=latency_ms,
                project_id=project_id,
            )
            total_cost += cost or 0.0
        await db.commit()

    return (
        ValidationResult(
            accept=accepts >= 2,
            confidence=avg_conf,
            votes=votes,
        ),
        total_cost,
    )
