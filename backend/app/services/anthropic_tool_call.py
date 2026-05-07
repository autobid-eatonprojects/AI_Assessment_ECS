"""Single Anthropic tool-use call with cache_control + cost logging.

The same shape — get client, optionally wrap tool with cache_control,
time the request, parse the first matching tool_use block, log cost
through `record_call` — appears in 5 places (sheet_index_extractor,
symbol_legend_extractor, spec_toc_extractor, conflict_resolution,
ragas_eval). Each used to carry ~50 lines of identical plumbing.

This module owns the plumbing once, so each call site only has to
declare what's specific to it: the model, the tool definition, the
user content, the purpose tag for cost attribution.

Design decisions:
  - Caller passes the raw `tool_def`; we add cache_control defensively
    (no-op if already present) without mutating the caller's dict.
  - Caller passes `system` as a plain string; we wrap it in the
    cache_control block shape Anthropic expects. `cache_system=False`
    skips caching for one-off prompts.
  - `user_content` accepts either a string (plain text) or a list of
    content blocks (image+text for vision sites).
  - Cost logging is internal — the caller doesn't need to know about
    SessionLocal or `record_call`. Pass `purpose=` so cost rolls up
    correctly in the LLMCall table.
  - Errors propagate; callers wrap if they want failure-tolerance
    semantics (e.g. symbol_legend returns [] on API error).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..database import SessionLocal
from .llm_log import record_call, usage_from_anthropic

log = logging.getLogger(__name__)


_client: Any = None


def _get_client() -> Any | None:
    """Process-wide singleton AsyncAnthropic, or None if no API key.

    Three former call sites each had their own copy of this function.
    Reuse this one. The Anthropic SDK is itself thread-safe and the
    AsyncClient is connection-pooled, so a single shared instance is
    correct.
    """
    global _client
    if _client is not None:
        return _client
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


@dataclass
class ToolCallResult:
    """Outcome of one Anthropic tool-use call.

    `parsed_input` is the dict the model passed to the tool — empty
    dict if the model didn't emit a tool_use block (rare with
    tool_choice forcing the tool, but possible on truncation).
    `cost_usd` is what `record_call` computed for this call. `raw_message`
    is the underlying Anthropic Message object, kept so callers can read
    fields the helper doesn't surface (e.g. stop_reason, usage breakdown).
    """

    parsed_input: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw_message: Any = None


async def call_with_tool(
    *,
    model: str,
    tool_def: dict,
    user_content: str | list[dict],
    purpose: str,
    system: str | None = None,
    max_tokens: int = 4096,
    cache_system: bool = True,
    project_id: str | None = None,
    document_id: str | None = None,
    page_extraction_id: str | None = None,
) -> ToolCallResult:
    """Run one Anthropic tool-use call and return the parsed payload + cost.

    The model is forced to call `tool_def["name"]` via tool_choice. The
    first matching tool_use block in the response becomes
    `result.parsed_input`. If the call goes through but no tool_use
    block matches (model emitted only text, hit max_tokens, etc.),
    `parsed_input` is an empty dict — caller decides how to react.

    Raises RuntimeError if no Anthropic API key is configured. Network
    / API errors propagate from the SDK.
    """
    client = _get_client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    tool_name = tool_def.get("name")
    if not tool_name:
        raise ValueError("tool_def missing required 'name' field")

    cached_tool = (
        tool_def
        if "cache_control" in tool_def
        else {**tool_def, "cache_control": {"type": "ephemeral"}}
    )

    request_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "tools": [cached_tool],
        "tool_choice": {"type": "tool", "name": tool_name},
    }
    if system is not None:
        request_kwargs["system"] = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if cache_system
            else system
        )

    if isinstance(user_content, str):
        request_kwargs["messages"] = [{"role": "user", "content": user_content}]
    else:
        request_kwargs["messages"] = [{"role": "user", "content": user_content}]

    t0 = time.perf_counter()
    msg = await client.messages.create(**request_kwargs)
    latency_ms = int((time.perf_counter() - t0) * 1000)

    parsed: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == tool_name
        ):
            parsed = block.input or {}
            break

    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose=purpose,
            model=model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
            document_id=document_id,
            page_extraction_id=page_extraction_id,
        )
        cost = float(c or 0.0)
        await db.commit()

    return ToolCallResult(
        parsed_input=parsed,
        cost_usd=cost,
        latency_ms=latency_ms,
        raw_message=msg,
    )
