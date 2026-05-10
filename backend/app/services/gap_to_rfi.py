"""Single-gap → RFI promotion (P9 companion).

The bulk RFI generator (rfi_generator.py) consolidates ALL of a project's
gaps into one Opus draft. This module handles the inverse: an operator
clicks "Promote to RFI" on ONE gap and gets back a single drafted RFI
they can copy into their RFI tool.

One Haiku call per gap (~$0.005). Audit-logged so the trail survives.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..config import settings
from ..database import SessionLocal
from ..models import AuditLog, Gap
from .llm_log import record_call, usage_from_anthropic

log = logging.getLogger(__name__)


_DRAFT_TOOL = {
    "name": "draft_one_rfi",
    "description": (
        "Draft ONE formal RFI from a single gap signal. The operator "
        "wants to send this to the design team — write the language an "
        "architect/engineer expects."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rfi_subject": {"type": "string"},
            "rfi_body": {
                "type": "string",
                "description": "Markdown body — issue, ask, impact",
            },
            "discipline": {"type": "string"},
            "csi_section": {"type": ["string", "null"]},
            "priority": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low"],
            },
            "sheet_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "rfi_subject", "rfi_body", "discipline", "priority", "sheet_refs",
        ],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT = """\
You are a senior General Contractor drafting ONE Request For \
Information for the design team. Given a gap signal, write a formal \
RFI in the language an architect or engineer expects.

Style:
  - Be SPECIFIC. Cite the CSI section, sheet IDs, and the actual ambiguity.
  - Phrase the ask as a clear question or directive.
  - Quantify impact when possible.
  - Priority guide:
      critical = blocks bid (must answer before invitations go out)
      high     = bid-affecting (cost or schedule impact > 5%)
      medium   = clarification needed but workable with assumptions
      low      = post-bid cleanup; raise but don't gate on it

Use draft_one_rfi when ready.
"""


def _client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def promote(project_id: str, gap: Gap, *, actor: str) -> dict:
    """Draft one RFI from a single Gap. Audit-logs the action."""
    client = _client()
    if client is None:
        return {
            "rfi_subject": "(no API key configured)",
            "rfi_body": gap.description,
            "discipline": "general",
            "csi_section": gap.csi_section,
            "priority": "medium",
            "sheet_refs": [],
        }

    user_prompt = (
        f"Project gap to promote to RFI:\n\n"
        f"Type: {gap.gap_type}\n"
        f"Severity: {gap.severity}\n"
        f"CSI: {gap.csi_section or gap.csi_division or '?'}\n"
        f"Description: {gap.description}\n"
        f"Suggested remediation: {gap.suggested_remediation or '(none)'}\n\n"
        f"Draft one RFI from this. Use draft_one_rfi."
    )

    t0 = time.perf_counter()
    try:
        msg = await client.messages.create(
            model=settings.classifier_model,  # Haiku 4.5
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_DRAFT_TOOL],
            tool_choice={"type": "tool", "name": "draft_one_rfi"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("gap_to_rfi: API error on gap %s: %s", gap.id, e)
        return {
            "rfi_subject": "(LLM error)",
            "rfi_body": gap.description,
            "discipline": "general",
            "csi_section": gap.csi_section,
            "priority": "medium",
            "sheet_refs": [],
        }
    latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "draft_one_rfi"
        ):
            payload = block.input
            break

    cost = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="gap-to-rfi",
            model=settings.classifier_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
        )
        cost = c or 0.0
        # Audit-log the promotion so the trail survives
        db.add(
            AuditLog(
                project_id=project_id,
                run_id=gap.run_id,
                entity_type="gap",
                entity_id=gap.id,
                action="promote_to_rfi",
                actor=actor,
                payload={
                    "gap_id": gap.id,
                    "gap_type": gap.gap_type,
                    "rfi_subject": payload.get("rfi_subject"),
                    "cost_usd": cost,
                    "latency_ms": latency_ms,
                },
                note="Operator promoted gap to RFI draft",
            )
        )
        await db.commit()

    log.info(
        "gap_to_rfi: gap %s → RFI '%s' in %dms ($%.4f)",
        gap.id, payload.get("rfi_subject"), latency_ms, cost,
    )
    return payload
