"""Gemini 2.5 Pro vision extractor — alternative to Claude Sonnet 4.6.

Same input/output contract as `vision_extractor.extract_page` so the processor
can swap providers via `settings.vision_provider`. Gemini supports native
JSON-schema constrained output (`response_mime_type='application/json'` +
`response_schema`), which is the equivalent of Claude's tool-use here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from ..config import settings
from ..schemas.extraction import PageExtractionIn
from .llm_log import Usage
from .vision_extractor import VisionResult, VisionUnavailable, _encode_for_vision

log = logging.getLogger(__name__)


_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.google_api_key:
        raise VisionUnavailable("GOOGLE_API_KEY is not set")
    from google import genai

    _client = genai.Client(api_key=settings.google_api_key)
    return _client


# JSON schema mirroring `PageExtractionIn`. Gemini constrains output to this.
_BBOX_SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "number"},
        "y": {"type": "number"},
        "width": {"type": "number"},
        "height": {"type": "number"},
    },
    "required": ["x", "y", "width", "height"],
}

_RESPONSE_SCHEMA = {
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
                        "civil",
                        "site",
                        "architectural",
                        "interior",
                        "structural",
                        "mechanical",
                        "plumbing",
                        "electrical",
                        "fire-protection",
                        "general",
                        "other",
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
                    "rows": {"type": "array", "items": {"type": "object"}},
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
                            "material",
                            "manufacturer",
                            "code",
                            "dimension",
                            "room",
                            "equipment",
                            "symbol",
                            "other",
                        ],
                    },
                    "value": {"type": "string"},
                    "bbox": _BBOX_SCHEMA,
                },
                "required": ["entity_type", "value"],
            },
        },
    },
}


PROMPT = """\
You are a senior construction estimator analysing one page of a construction \
drawing set. Your output goes directly into an estimating database used to \
build a Scope of Work and to verify subcontractor bids, so accuracy and \
completeness matter.

Return a JSON object matching the schema. Be exhaustive.

What to extract
---------------
1. sheet_metadata — sheet_number (e.g. "S1.1", "A1.1", "M0.1"), sheet_title, \
discipline, drawing_scale (e.g. '1/4" = 1\\'-0"' or "AS NOTED").

2. schedules — every TABLE on the page (footing schedule, door schedule, \
finish schedule, fixture schedule, equipment schedule, sprinkler head \
legend, etc.). For each: name, columns (header text verbatim), and ALL rows \
as objects keyed by the column headers. Don't paraphrase row values. Omit \
empty cells.

3. notes — every paragraph of "GENERAL NOTES", code references, \
contractor responsibilities, or similar narrative text. One paragraph per \
note item.

4. cross_references — pointers to OTHER sheets or details, e.g. \
"see S2.1", "detail 5/A5.2". Set target_sheet, detail_id (if any), and \
the context snippet from the drawing.

5. entities — fine-grained tagged identifiers visible on the page:
   - material: "4-inch concrete slab", "8\\" CMU"
   - manufacturer: "TYCO TY3121", "VICTAULIC 717", "Wilsonart 1573SL"
   - code: "NFPA 13 2019", "ASTM E1264", "IBC 2024", "SMACNA"
   - dimension: explicit measurements ("8'-0\\"", "5,200 SF")
   - room: visible room labels on plans ("CONFERENCE 201", "STORAGE B-12")
   - equipment: equipment tags ("RTU-1", "AHU-2", "F6.0")
   - symbol: legend symbols with their meaning
   - other: anything else worth recording

Bounding boxes
--------------
Every item with spatial location should include a bbox in NORMALISED page \
coordinates [0, 1]: (0, 0) is top-left, (1, 1) is bottom-right.

If something isn't on this page, return an empty array — don't invent data.
"""


def _read_image_bytes(image_path: Path) -> tuple[bytes, str]:
    """Return (bytes, mime_type) re-encoded if needed.

    Re-uses the same _encode_for_vision logic from the Anthropic extractor so
    Gemini gets the exact same input. Gemini's image limit is generous (20 MB
    per image) but we keep the pipeline consistent.
    """
    raw = image_path.read_bytes()
    if len(raw) <= 4 * 1024 * 1024:
        return raw, "image/png"
    # Re-use the smarter re-encode logic.
    import base64

    data, media_type = _encode_for_vision(image_path)
    return base64.b64decode(data), media_type


async def _resize_for_gemini_if_needed(image_path: Path):
    """Returns a (bytes, mime_type) pair suitable for Gemini ingestion.

    Gemini accepts up to 20 MB per inline image — our 150-DPI PNGs at ~5 MB
    are fine, but we still re-encode on the few oversize cases for parity
    with the Anthropic path.
    """
    return await asyncio.to_thread(_read_image_bytes, image_path)


async def extract_page(
    image_path: Path,
    *,
    page_number: int,
    document_filename: str | None = None,
) -> VisionResult:
    """Run vision pre-pass on a single page using Gemini 2.5 Pro."""
    client = _get_client()
    img_bytes, mime_type = await _resize_for_gemini_if_needed(image_path)

    last_error: Exception | None = None
    for attempt in range(settings.vision_max_retries + 1):
        t0 = time.perf_counter()
        log.info(
            "gemini-vision: extracting page %d (attempt %d/%d) of %s",
            page_number,
            attempt + 1,
            settings.vision_max_retries + 1,
            document_filename or "<unknown>",
        )

        prompt_text = PROMPT
        if last_error is not None:
            prompt_text += (
                f"\n\nYour previous response failed schema validation:\n{last_error}\n"
                "Return a corrected JSON object."
            )

        from google.genai import types as gtypes

        content = [
            gtypes.Part.from_bytes(data=img_bytes, mime_type=mime_type),
            gtypes.Part.from_text(
                text=(
                    f"Page {page_number}"
                    + (f" of {document_filename}" if document_filename else "")
                    + "\n\n"
                    + prompt_text
                )
            ),
        ]

        try:
            resp = await client.aio.models.generate_content(
                model=settings.gemini_vision_model,
                contents=content,
                config=gtypes.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                    max_output_tokens=16384,
                ),
            )
        except Exception as e:  # noqa: BLE001
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log.warning(
                "gemini-vision: API error on page %d attempt %d (%dms): %s",
                page_number,
                attempt + 1,
                latency_ms,
                e,
            )
            last_error = e
            if attempt < settings.vision_max_retries:
                await asyncio.sleep(min(2**attempt, 8))
                continue
            raise

        latency_ms = int((time.perf_counter() - t0) * 1000)

        # Token usage
        usage_meta = getattr(resp, "usage_metadata", None)
        usage = Usage(
            prompt_tokens=getattr(usage_meta, "prompt_token_count", 0) or 0,
            completion_tokens=getattr(usage_meta, "candidates_token_count", 0) or 0,
        )

        # Gemini returns parsed JSON when response_mime_type is JSON
        parsed = None
        try:
            parsed = resp.parsed
        except Exception:  # noqa: BLE001
            parsed = None
        if parsed is None:
            try:
                import json as _json

                parsed = _json.loads(resp.text)
            except Exception as e:  # noqa: BLE001
                last_error = RuntimeError(f"gemini returned non-JSON: {e}")
                if attempt < settings.vision_max_retries:
                    continue
                raise last_error

        try:
            extraction = PageExtractionIn.model_validate(parsed)
        except ValidationError as e:
            last_error = e
            log.warning(
                "gemini-vision: schema validation failed on page %d attempt %d: %s",
                page_number,
                attempt + 1,
                e,
            )
            if attempt < settings.vision_max_retries:
                continue
            raise

        return VisionResult(
            extraction=extraction,
            raw_response=parsed if isinstance(parsed, dict) else dict(parsed),
            usage=usage,
            latency_ms=latency_ms,
            model=settings.gemini_vision_model,
        )

    assert last_error is not None
    raise last_error
