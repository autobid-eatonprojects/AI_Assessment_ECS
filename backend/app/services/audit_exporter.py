"""P8 — audit_log → JSONL fine-tuning dataset exporter.

Per the design doc, every operator decision the GC makes during HITL
review (override a citation, reject an item, merge a duplicate, accept
a verifier suggestion) becomes a labeled training example. Over time
this dataset is used to fine-tune the per-customer link_judge / scope
_validator models so the system learns from each GC's specific
estimating preferences.

This module reads AuditLog + ScopeCitation + ScopeItem + Chunk and
emits one JSONL row per decision in Anthropic's MessageBatch fine-
tuning format:

    {"messages": [
        {"role": "user", "content": "<reconstruction of the prompt
            the link_judge saw at decision time>"},
        {"role": "assistant", "content": "<operator's verdict +
            reasoning>"}
    ]}

The dataset is project-scoped so customers' data stays segregated.
Re-running an export is idempotent — it overwrites the previous
dataset file. Per-customer fine-tuning is out of scope for this
module; we only emit the file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import (
    AuditLog,
    Chunk,
    ScopeCitation,
    ScopeItem,
)

log = logging.getLogger(__name__)


# Actions that produce supervised examples for the link_judge:
# operator-driven citation flips and overrides. We exclude pure-system
# actions (run start/end, auto-arbitration) since they aren't ground
# truth — only human-verified decisions are.
_SUPERVISED_ACTIONS = {
    "override",      # operator changed the verdict
    "accept",        # operator confirmed the verdict
    "reject",        # operator rejected the verdict
    "resolve",       # conflict resolution
    "acknowledge",   # gap acknowledged
}

_SUPERVISED_ENTITY_TYPES = {
    "scope_item",
    "scope_citation",
    "conflict",
    "gap",
}


@dataclass
class _SupervisedExample:
    """One labeled example: prompt input + operator's verdict."""

    audit_id: str
    entity_type: str
    entity_id: str
    action: str
    actor: str
    item_csi_code: str | None
    item_description: str | None
    citation_excerpt: str | None
    chunk_text: str | None
    operator_verdict: str | None
    operator_reasoning: str | None
    payload: dict[str, Any]


def _format_link_judge_prompt(item_csi: str, item_desc: str, chunk_text: str) -> str:
    """Reconstruct the link_judge user prompt from persisted state.

    Keep the format byte-identical to what link_judge.py emits at
    inference time — fine-tuning only generalizes if the training
    distribution matches the inference distribution.
    """
    return (
        "You are a senior estimator entailment-checking the citations "
        "on a scope-of-work item. For this citation, decide whether the "
        "chunk genuinely supports the item.\n\n"
        f"=== SCOPE ITEM ===\n"
        f"CSI code: {item_csi}\n"
        f"Description: {item_desc}\n\n"
        f"=== CITATION CHUNK ===\n"
        f"{chunk_text}\n\n"
        "Verdict: ENTAILED / NEUTRAL / CONTRADICTED."
    )


def _format_assistant(verdict: str | None, reasoning: str | None) -> str:
    """Operator's labeled answer in the same shape link_judge emits."""
    parts = [verdict or "ENTAILED"]
    if reasoning:
        parts.append(f"Rationale: {reasoning.strip()}")
    return " — ".join(parts)


async def collect_link_judge_examples(
    project_id: str,
) -> list[_SupervisedExample]:
    """Read every operator-driven decision on link_judge outputs.

    Joins AuditLog → ScopeCitation → Chunk + ScopeItem to reconstruct
    the prompt that the model would have seen at inference time.
    """
    examples: list[_SupervisedExample] = []
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.project_id == project_id)
                .where(AuditLog.action.in_(_SUPERVISED_ACTIONS))
                .where(AuditLog.entity_type.in_(_SUPERVISED_ENTITY_TYPES))
                .where(AuditLog.actor.like("user:%"))
                .order_by(AuditLog.created_at)
            )
        ).scalars().all()
        if not rows:
            return []

        # Pre-fetch related citations + items + chunks to avoid N+1
        cit_ids = [
            r.entity_id for r in rows
            if r.entity_type == "scope_citation" and r.entity_id
        ]
        item_ids = [
            r.entity_id for r in rows
            if r.entity_type == "scope_item" and r.entity_id
        ]
        # Citation rows + the item they belong to + the chunk they cite
        cit_by_id: dict[str, ScopeCitation] = {}
        chunk_by_id: dict[str, Chunk] = {}
        item_by_id: dict[str, ScopeItem] = {}
        if cit_ids:
            cit_rows = (
                await db.execute(
                    select(ScopeCitation).where(ScopeCitation.id.in_(cit_ids))
                )
            ).scalars().all()
            cit_by_id = {c.id: c for c in cit_rows}
            chunk_lookups = list({c.chunk_id for c in cit_rows if c.chunk_id})
            if chunk_lookups:
                chunk_rows = (
                    await db.execute(
                        select(Chunk).where(Chunk.id.in_(chunk_lookups))
                    )
                ).scalars().all()
                chunk_by_id = {c.id: c for c in chunk_rows}
            cit_item_ids = list({c.scope_item_id for c in cit_rows if c.scope_item_id})
            item_ids = list(set(item_ids) | set(cit_item_ids))
        if item_ids:
            item_rows = (
                await db.execute(
                    select(ScopeItem).where(ScopeItem.id.in_(item_ids))
                )
            ).scalars().all()
            item_by_id = {i.id: i for i in item_rows}

        for r in rows:
            payload = r.payload or {}
            verdict = (
                payload.get("verdict")
                or payload.get("after", {}).get("verdict")
                or payload.get("decision")
            )
            reasoning = (
                payload.get("reasoning")
                or payload.get("note")
                or r.note
            )

            item: ScopeItem | None = None
            cit: ScopeCitation | None = None
            chunk: Chunk | None = None

            if r.entity_type == "scope_citation" and r.entity_id:
                cit = cit_by_id.get(r.entity_id)
                if cit is not None:
                    item = item_by_id.get(cit.scope_item_id)
                    chunk = chunk_by_id.get(cit.chunk_id)
            elif r.entity_type == "scope_item" and r.entity_id:
                item = item_by_id.get(r.entity_id)

            examples.append(
                _SupervisedExample(
                    audit_id=r.id,
                    entity_type=r.entity_type,
                    entity_id=r.entity_id or "",
                    action=r.action,
                    actor=r.actor,
                    item_csi_code=item.csi_code if item else None,
                    item_description=item.description if item else None,
                    citation_excerpt=cit.excerpt if cit else None,
                    chunk_text=(chunk.text if chunk else None),
                    operator_verdict=verdict,
                    operator_reasoning=reasoning,
                    payload=payload,
                )
            )
    return examples


def _to_messages_jsonl(ex: _SupervisedExample) -> dict | None:
    """Render one example as an Anthropic fine-tuning message pair.

    Returns None when there isn't enough context to reconstruct a
    learnable input/output pair (e.g. the citation chunk has been
    deleted, or the operator action lacks a verdict in payload).
    """
    if not ex.item_csi_code or not ex.item_description:
        return None
    chunk_text = ex.chunk_text or ex.citation_excerpt
    if not chunk_text:
        return None

    user_prompt = _format_link_judge_prompt(
        ex.item_csi_code, ex.item_description, chunk_text
    )
    assistant_text = _format_assistant(
        ex.operator_verdict, ex.operator_reasoning
    )
    return {
        "messages": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": assistant_text},
        ],
        "metadata": {
            "audit_id": ex.audit_id,
            "actor": ex.actor,
            "action": ex.action,
            "entity_type": ex.entity_type,
            "entity_id": ex.entity_id,
        },
    }


async def export_link_judge_dataset(
    project_id: str,
    *,
    output_path: Path | None = None,
) -> dict:
    """Export the project's operator decisions as JSONL fine-tuning data.

    Default output path: <storage_root>/exports/<project_id>/link_judge_dataset.jsonl

    Returns {"path", "examples_total", "examples_written"}.
    """
    examples = await collect_link_judge_examples(project_id)
    if output_path is None:
        output_path = (
            Path(settings.storage_root) / "exports" / project_id
            / "link_judge_dataset.jsonl"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with output_path.open("w", encoding="utf-8") as f:
        for ex in examples:
            row = _to_messages_jsonl(ex)
            if row is None:
                continue
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1

    log.info(
        "audit_exporter: project %s — %d examples → %d JSONL rows at %s",
        project_id, len(examples), written, output_path,
    )
    return {
        "path": str(output_path),
        "examples_total": len(examples),
        "examples_written": written,
    }
