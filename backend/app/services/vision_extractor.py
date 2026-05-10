"""Per-page vision pre-pass — provider-pluggable.

Sends a rendered drawing-set page to a vision LLM with a strict schema
(sheet metadata, schedules, notes, cross-references, entities) and returns
a validated `PageExtractionIn` plus token usage so the caller can persist
the extraction and the cost log atomically.

Architecture
------------
The Module owns:
  - The output schema (mirrors `PageExtractionIn`)
  - The estimator-grade prompt text
  - The retry-on-validation-error loop (one corrective re-prompt)
  - Image encoding (with re-encoding to fit per-provider size limits)
  - Result validation

Provider adapters provide ONLY the API call shape:
  - AnthropicVisionAdapter — Sonnet 4.6 via tool-use schema constraint
  - GeminiVisionAdapter   — Gemini 2.5 Pro/Flash via response-schema JSON

This deepening replaces the previous parallel-sibling design where
`vision_extractor.py` (Sonnet) and `gemini_vision_extractor.py` (Gemini)
each reimplemented schema, prompt, retry, validation, and image encoding.
Adding a third provider (e.g. GPT-4o) is now a ~50-LoC adapter, not a
~600-LoC duplication.

Design notes
------------
- Bounding boxes are NORMALISED [0, 1] so they're independent of render
  DPI and survive frontend resizing.
- The prompt is project-agnostic (pure CSI / industry standard) so the
  same pipeline works for any construction drawing set.
- Adapter selection is via `settings.vision_provider` ("anthropic" |
  "google"); callers can also pass `provider=` explicitly for tests.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import ValidationError

from ..config import settings
from ..schemas.extraction import PageExtractionIn
from .llm_log import Usage, usage_from_anthropic

log = logging.getLogger(__name__)


class VisionUnavailable(Exception):
    """Raised when the configured provider has no API key."""


# =============================================================================
# Schema — single source of truth, mirrors PageExtractionIn
# =============================================================================


_BBOX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "x": {"type": "number", "minimum": 0, "maximum": 1},
        "y": {"type": "number", "minimum": 0, "maximum": 1},
        "width": {"type": "number", "minimum": 0, "maximum": 1, "exclusiveMinimum": 0},
        "height": {"type": "number", "minimum": 0, "maximum": 1, "exclusiveMinimum": 0},
    },
    "required": ["x", "y", "width", "height"],
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sheet_metadata": {
            "type": "object",
            "properties": {
                "sheet_number": {"type": "string"},
                "sheet_title": {"type": "string"},
                "discipline": {
                    "type": "string",
                    "enum": [
                        "civil", "site", "architectural", "interior", "structural",
                        "mechanical", "plumbing", "electrical", "fire-protection",
                        "general", "other",
                    ],
                },
                "drawing_scale": {"type": "string"},
            },
        },
        "schedules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                    "bbox": _BBOX_SCHEMA,
                },
                "required": ["name", "columns", "rows"],
            },
        },
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "bbox": _BBOX_SCHEMA,
                },
                "required": ["text"],
            },
        },
        "cross_references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_sheet": {"type": "string"},
                    "detail_id": {"type": "string"},
                    "context": {"type": "string"},
                    "bbox": _BBOX_SCHEMA,
                },
                "required": ["target_sheet"],
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": [
                            "material", "manufacturer", "code", "dimension",
                            "room", "equipment", "symbol", "other",
                        ],
                    },
                    "value": {"type": "string"},
                    "bbox": _BBOX_SCHEMA,
                    "extra": {"type": "object", "additionalProperties": True},
                },
                "required": ["entity_type", "value"],
            },
        },
    },
}


# Anthropic's tool form wraps the schema in a tool definition.
_ANTHROPIC_TOOL: dict[str, Any] = {
    "name": "extract_page",
    "description": (
        "Extract every structured element from one page of a construction "
        "drawing set. All bounding boxes use NORMALISED page coordinates "
        "in [0, 1] where (0,0) is top-left."
    ),
    "input_schema": _OUTPUT_SCHEMA,
}


# =============================================================================
# Prompt — single source of truth
# =============================================================================


SYSTEM_PROMPT = """\
You are a senior construction estimator analysing one page of a construction \
drawing set. Your output goes directly into an estimating database used to \
build a Scope of Work and to verify subcontractor bids, so accuracy and \
completeness matter.

Be exhaustive.

What to extract
---------------
1. **sheet_metadata** — sheet_number (e.g. "S1.1", "A1.1", "M0.1"), \
sheet_title, discipline, drawing_scale (e.g. "1/4\\" = 1'-0\\"" or \
"AS NOTED").

2. **schedules** — every TABLE on the page (footing schedule, door schedule, \
finish schedule, fixture schedule, equipment schedule, sprinkler head \
legend, etc.). For each: extract its `name`, `columns` (header text \
verbatim), and ALL `rows` as objects keyed by the column headers. Don't \
paraphrase row values. If a cell is empty, omit the key from that row.

3. **notes** — every paragraph of "GENERAL NOTES", code references, \
contractor responsibilities, or similar narrative text. One paragraph per \
note item.

4. **cross_references** — pointers to OTHER sheets or details, e.g. \
"see S2.1", "detail 5/A5.2", "see Architectural for finish schedule". \
Set `target_sheet` (e.g. "S2.1"), `detail_id` if present (e.g. "5"), and \
the `context` snippet from the drawing.

5. **entities** — fine-grained tagged identifiers visible on the page:
   - material: e.g. concrete-slab thicknesses, CMU sizes, insulation R-values
   - manufacturer: any brand name + model/spec combination (e.g. brand of
     sprinkler head, fitting, finish laminate). Capture verbatim.
   - code: any cited standard or code edition (e.g. "NFPA 13 2019",
     "ASTM E1264", "IBC 2024", "SMACNA")
   - dimension: any explicit measurement worth tagging
   - room: room/zone labels visible on plans
   - equipment: equipment tags (e.g. "RTU-1", "AHU-2", or schedule mark IDs)
   - symbol: legend symbols with their meaning
   - other: anything else worth recording

Bounding boxes
--------------
Every item with spatial location should include a `bbox` in NORMALISED page \
coordinates [0, 1]: (0, 0) is top-left, (1, 1) is bottom-right.

If you genuinely can't see something on this page, return an empty array \
for that field — do not invent data. Be exhaustive on what IS there.
"""


# =============================================================================
# VisionResult — single source of truth
# =============================================================================


@dataclass
class VisionResult:
    extraction: PageExtractionIn
    raw_response: dict[str, Any]
    usage: Usage
    latency_ms: int
    model: str


# =============================================================================
# Image encoding — shared
# =============================================================================


# Anthropic's images endpoint caps the base64-encoded payload at 5 MiB.
# Base64 inflates raw bytes by ~33%, so the raw image must stay under
# ~3.75 MiB. Dense pages (cover sheets, structural notes) overshoot at
# 150 DPI PNG; JPEG quality 85 is visually identical and 3-5x smaller.
# Gemini's per-image limit is much higher (20 MiB) but we use the same
# budget for parity — the image quality at this size is ample for both.
_RAW_BUDGET_BYTES = int(3.75 * 1024 * 1024)


def _encode_for_vision(image_path: Path) -> tuple[bytes, str]:
    """Return (raw_bytes, media_type), re-encoding only if needed.

    PNG passes through when small; otherwise progressive JPEG re-encoding
    (preserve quality first, then downscale) until we fit the budget.
    """
    raw = image_path.read_bytes()
    if len(raw) <= _RAW_BUDGET_BYTES:
        return raw, "image/png"

    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")

    for scale, quality in [
        (1.0, 90), (1.0, 85), (1.0, 80),
        (0.8, 85), (0.65, 85), (0.5, 85),
    ]:
        candidate = (
            img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
            if scale < 1.0
            else img
        )
        buf = io.BytesIO()
        candidate.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= _RAW_BUDGET_BYTES:
            log.info(
                "vision: re-encoded %s from %d B PNG to %d B JPEG (scale=%.2f, q=%d)",
                image_path.name, len(raw), buf.tell(), scale, quality,
            )
            return buf.getvalue(), "image/jpeg"

    # Last-ditch aggressive downscale.
    candidate = img.resize((int(img.width * 0.4), int(img.height * 0.4)), Image.LANCZOS)
    buf = io.BytesIO()
    candidate.save(buf, format="JPEG", quality=80, optimize=True)
    log.warning(
        "vision: forced aggressive downscale on %s to %d B",
        image_path.name, buf.tell(),
    )
    return buf.getvalue(), "image/jpeg"


# =============================================================================
# Adapter interface
# =============================================================================


@dataclass
class _AdapterCallResult:
    """What every adapter returns from one API call."""
    parsed_output: dict[str, Any] | None
    usage: Usage
    latency_ms: int
    model: str


class VisionAdapter(ABC):
    """Provider-pluggable vision call. The Module owns schema, prompt,
    retries, image encoding, validation. The adapter owns ONLY the API
    call shape and response unwrapping."""

    @abstractmethod
    async def call(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        user_text: str,
        retry_hint: str | None,
    ) -> _AdapterCallResult:
        """Run one vision API call and return the parsed JSON output dict."""


# =============================================================================
# Anthropic adapter — Sonnet 4.6 via tool-use
# =============================================================================


class AnthropicVisionAdapter(VisionAdapter):
    def __init__(self) -> None:
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not settings.anthropic_api_key:
            raise VisionUnavailable("ANTHROPIC_API_KEY is not set")
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        return self._client

    async def call(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        user_text: str,
        retry_hint: str | None,
    ) -> _AdapterCallResult:
        client = self._get_client()
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        user_blocks: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": b64},
            },
            {"type": "text", "text": user_text},
        ]
        if retry_hint is not None:
            user_blocks.append({"type": "text", "text": retry_hint})

        t0 = time.perf_counter()
        message = await client.messages.create(
            model=settings.vision_model,
            # Dense pages (e.g. S0.1 with 5 schedules, 80+ rows) need
            # plenty of room. 16384 is well within Sonnet 4.6's 64k cap.
            max_tokens=16384,
            # Cache the system prompt + tool schema so every page in a
            # drawing set reuses the cached prefix.
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT
                    + "\n\nUse the `extract_page` tool exactly once.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_ANTHROPIC_TOOL],
            tool_choice={"type": "tool", "name": "extract_page"},
            messages=[{"role": "user", "content": user_blocks}],
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        usage = usage_from_anthropic(message)

        parsed: dict[str, Any] | None = None
        for block in getattr(message, "content", []) or []:
            if (
                getattr(block, "type", None) == "tool_use"
                and block.name == "extract_page"
            ):
                parsed = block.input
                break

        return _AdapterCallResult(
            parsed_output=parsed,
            usage=usage,
            latency_ms=latency_ms,
            model=settings.vision_model,
        )


# =============================================================================
# Gemini adapter — 2.5 Pro/Flash via response-schema JSON
# =============================================================================


class GeminiVisionAdapter(VisionAdapter):
    def __init__(self) -> None:
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not settings.google_api_key:
            raise VisionUnavailable("GOOGLE_API_KEY is not set")
        from google import genai
        self._client = genai.Client(api_key=settings.google_api_key)
        return self._client

    async def call(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        user_text: str,
        retry_hint: str | None,
    ) -> _AdapterCallResult:
        client = self._get_client()
        from google.genai import types as gtypes

        prompt_text = (
            SYSTEM_PROMPT
            + "\n\nReturn a JSON object matching the schema."
            + f"\n\n{user_text}"
        )
        if retry_hint is not None:
            prompt_text += f"\n\n{retry_hint}"

        content = [
            gtypes.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            gtypes.Part.from_text(text=prompt_text),
        ]

        t0 = time.perf_counter()
        resp = await client.aio.models.generate_content(
            model=settings.gemini_vision_model,
            contents=content,
            config=gtypes.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_OUTPUT_SCHEMA,
                max_output_tokens=16384,
            ),
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

        usage_meta = getattr(resp, "usage_metadata", None)
        usage = Usage(
            prompt_tokens=getattr(usage_meta, "prompt_token_count", 0) or 0,
            completion_tokens=getattr(usage_meta, "candidates_token_count", 0) or 0,
        )

        parsed: dict[str, Any] | None = None
        try:
            parsed = resp.parsed
        except Exception:  # noqa: BLE001
            parsed = None
        if parsed is None:
            try:
                import json as _json
                parsed = _json.loads(resp.text)
            except Exception:  # noqa: BLE001
                parsed = None

        if parsed is not None and not isinstance(parsed, dict):
            try:
                parsed = dict(parsed)
            except Exception:  # noqa: BLE001
                parsed = None

        return _AdapterCallResult(
            parsed_output=parsed,
            usage=usage,
            latency_ms=latency_ms,
            model=settings.gemini_vision_model,
        )


# =============================================================================
# Adapter selector
# =============================================================================


def _get_adapter(provider: str | None = None) -> VisionAdapter:
    """Return a vision adapter instance for the requested provider.

    `provider=None` → use settings.vision_provider. Pass a value explicitly
    for tests (e.g. a fake adapter constructed inline).
    """
    name = (provider or settings.vision_provider or "anthropic").strip().lower()
    if name in ("anthropic", "claude", "sonnet"):
        return AnthropicVisionAdapter()
    if name in ("google", "gemini"):
        return GeminiVisionAdapter()
    raise ValueError(
        f"unknown vision_provider {name!r}; "
        f"expected 'anthropic' or 'google'"
    )


# =============================================================================
# Public surface — extract_page
# =============================================================================


async def extract_page(
    image_path: Path,
    *,
    page_number: int,
    document_filename: str | None = None,
    adapter: VisionAdapter | None = None,
) -> VisionResult:
    """Run the vision pre-pass on a single page.

    Schema-validated, with one corrective retry on validation failure or
    transient API errors.

    Parameters
    ----------
    image_path : Path
        On-disk PNG/JPEG of the rendered page.
    page_number : int
        1-based page number, included in the user message for context.
    document_filename : str | None
        Source PDF filename, included in the user message for context.
    adapter : VisionAdapter | None
        Override the configured provider. When None, use
        ``settings.vision_provider`` to select. Pass a fake adapter for
        tests — that's the test surface.
    """
    chosen_adapter = adapter if adapter is not None else _get_adapter()

    # Image encoding (off-thread; PIL is sync).
    image_bytes, mime_type = await asyncio.to_thread(_encode_for_vision, image_path)

    user_text = (
        f"Page {page_number}"
        + (f" of {document_filename}" if document_filename else "")
        + ". Extract every structured element."
    )

    last_error: Exception | None = None
    for attempt in range(settings.vision_max_retries + 1):
        log.info(
            "vision: extracting page %d (attempt %d/%d, adapter=%s) of %s",
            page_number,
            attempt + 1,
            settings.vision_max_retries + 1,
            type(chosen_adapter).__name__,
            document_filename or "<unknown>",
        )

        retry_hint: str | None = None
        if last_error is not None:
            retry_hint = (
                f"Your previous response failed schema validation:\n\n"
                f"{last_error}\n\nReturn a corrected response."
            )

        try:
            result = await chosen_adapter.call(
                image_bytes=image_bytes,
                mime_type=mime_type,
                user_text=user_text,
                retry_hint=retry_hint,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "vision: API error on page %d attempt %d: %s",
                page_number, attempt + 1, e,
            )
            last_error = e
            if attempt < settings.vision_max_retries:
                await asyncio.sleep(min(2**attempt, 8))
                continue
            raise

        if result.parsed_output is None:
            last_error = RuntimeError(
                f"vision: adapter {type(chosen_adapter).__name__} returned no "
                f"parsed output on page {page_number}"
            )
            if attempt < settings.vision_max_retries:
                continue
            raise last_error

        try:
            extraction = PageExtractionIn.model_validate(result.parsed_output)
        except ValidationError as e:
            last_error = e
            log.warning(
                "vision: schema validation failed on page %d attempt %d: %s",
                page_number, attempt + 1, e,
            )
            if attempt < settings.vision_max_retries:
                continue
            raise

        return VisionResult(
            extraction=extraction,
            raw_response=result.parsed_output,
            usage=result.usage,
            latency_ms=result.latency_ms,
            model=result.model,
        )

    # Defensive — loop should always either return or raise.
    assert last_error is not None
    raise last_error
