"""Per-page vision pre-pass with Claude Sonnet 4.6.

Sends a rendered drawing page to Claude with a strict tool-use schema covering
sheet metadata, schedules, general notes, cross-references, and tagged
entities. Returns a validated `PageExtractionIn` plus token usage so callers
can persist the extraction and the cost log atomically.

Design notes
------------
- The tool-use schema mirrors `app.schemas.extraction.PageExtractionIn` so
  Claude's output is validated as Pydantic before it ever touches the DB.
- One automatic retry on Pydantic validation failure: we feed the validation
  error back to Claude with a corrective prompt. Empirically this fixes most
  small schema mistakes without burning the whole job.
- The prompt is project-agnostic (pure CSI / industry standard) so the same
  pipeline works for any construction drawing set.
- Bounding boxes are NORMALISED [0, 1] so they're independent of render DPI
  and survive resizing in the frontend.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image
from pydantic import ValidationError

from ..config import settings
from ..schemas.extraction import PageExtractionIn
from .llm_log import Usage, usage_from_anthropic

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)


class VisionUnavailable(Exception):
    """Raised when no Anthropic API key is configured."""


_client: "AsyncAnthropic | None" = None


def _get_client() -> "AsyncAnthropic":
    global _client
    if _client is not None:
        return _client
    if not settings.anthropic_api_key:
        raise VisionUnavailable("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


# -----------------------------------------------------------------------------
# Tool-use schema. Kept aligned with PageExtractionIn.
# -----------------------------------------------------------------------------

_BBOX_SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "number", "minimum": 0, "maximum": 1},
        "y": {"type": "number", "minimum": 0, "maximum": 1},
        "width": {"type": "number", "minimum": 0, "maximum": 1, "exclusiveMinimum": 0},
        "height": {"type": "number", "minimum": 0, "maximum": 1, "exclusiveMinimum": 0},
    },
    "required": ["x", "y", "width", "height"],
}

_EXTRACT_TOOL: dict[str, Any] = {
    "name": "extract_page",
    "description": (
        "Extract every structured element from one page of a construction drawing set. "
        "All bounding boxes use NORMALISED page coordinates in [0, 1] where (0,0) is "
        "top-left."
    ),
    "input_schema": {
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
                        "extra": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["entity_type", "value"],
                },
            },
        },
    },
}


SYSTEM_PROMPT = """\
You are a senior construction estimator analysing one page of a construction \
drawing set. Your output goes directly into an estimating database used to \
build a Scope of Work and to verify subcontractor bids, so accuracy and \
completeness matter.

Use the `extract_page` tool exactly once. Be exhaustive.

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
   - material: "4-inch concrete slab", "8\\" CMU", "R-19 batt insulation"
   - manufacturer: "TYCO TY3121", "VICTAULIC 717", "Wilsonart 1573SL"
   - code: "NFPA 13 2019", "ASTM E1264", "IBC 2024", "SMACNA"
   - dimension: any explicit measurement worth tagging ("8'-0\\"", "5,200 SF")
   - room: room/zone labels visible on plans ("CONFERENCE 201", "STORAGE B-12")
   - equipment: equipment tags ("RTU-1", "AHU-2", "F6.0")
   - symbol: legend symbols with their meaning
   - other: anything else worth recording

Bounding boxes
--------------
Every item with spatial location should include a `bbox` in NORMALISED page \
coordinates [0, 1]: (0, 0) is top-left, (1, 1) is bottom-right.

If you genuinely can't see something on this page, return an empty array \
for that field — do not invent data. Be exhaustive on what IS there.

Use the `extract_page` tool now.
"""


@dataclass
class VisionResult:
    extraction: PageExtractionIn
    raw_response: dict[str, Any]
    usage: Usage
    latency_ms: int
    model: str


# Anthropic's images endpoint caps the base64-encoded payload at 5 MiB.
# Base64 inflates raw bytes by ~33%, so the raw image must stay under
# ~3.75 MiB. Dense pages (cover sheets, structural notes) overshoot at
# 150 DPI PNG; JPEG quality 85 is visually identical and 3–5x smaller.
_RAW_BUDGET_BYTES = int(3.75 * 1024 * 1024)


def _encode_for_vision(image_path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type), re-encoding only if needed."""
    raw = image_path.read_bytes()
    if len(raw) <= _RAW_BUDGET_BYTES:
        return base64.standard_b64encode(raw).decode("ascii"), "image/png"

    # Re-encode as JPEG. Try decreasing quality, then progressive downsizing.
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")

    for scale, quality in [
        (1.0, 90),
        (1.0, 85),
        (1.0, 80),
        (0.8, 85),
        (0.65, 85),
        (0.5, 85),
    ]:
        if scale < 1.0:
            new_size = (int(img.width * scale), int(img.height * scale))
            candidate = img.resize(new_size, Image.LANCZOS)
        else:
            candidate = img
        buf = io.BytesIO()
        candidate.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= _RAW_BUDGET_BYTES:
            log.info(
                "vision: re-encoded %s from %d B PNG to %d B JPEG (scale=%.2f, q=%d)",
                image_path.name,
                len(raw),
                buf.tell(),
                scale,
                quality,
            )
            return base64.standard_b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"

    # Last-ditch: very aggressive downscale. Better than failing the page.
    candidate = img.resize((int(img.width * 0.4), int(img.height * 0.4)), Image.LANCZOS)
    buf = io.BytesIO()
    candidate.save(buf, format="JPEG", quality=80, optimize=True)
    log.warning(
        "vision: forced aggressive downscale on %s to %d B", image_path.name, buf.tell()
    )
    return base64.standard_b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _image_block(image_path: Path) -> dict[str, Any]:
    data, media_type = _encode_for_vision(image_path)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _extract_tool_use_block(message) -> dict[str, Any] | None:
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_page":
            return block.input
    return None


async def extract_page(
    image_path: Path,
    *,
    page_number: int,
    document_filename: str | None = None,
) -> VisionResult:
    """Run vision pre-pass on a single page. Schema-validated, with one retry."""
    client = _get_client()

    base_user_blocks: list[dict[str, Any]] = [
        _image_block(image_path),
        {
            "type": "text",
            "text": (
                f"Page {page_number}"
                + (f" of {document_filename}" if document_filename else "")
                + ". Extract every structured element using the `extract_page` tool."
            ),
        },
    ]

    last_error: Exception | None = None

    for attempt in range(settings.vision_max_retries + 1):
        t0 = time.perf_counter()
        log.info(
            "vision: extracting page %d (attempt %d/%d) of %s",
            page_number,
            attempt + 1,
            settings.vision_max_retries + 1,
            document_filename or "<unknown>",
        )
        user_blocks = list(base_user_blocks)
        if last_error is not None:
            user_blocks.append(
                {
                    "type": "text",
                    "text": (
                        "Your previous response failed schema validation with this "
                        f"error:\n\n{last_error}\n\nReturn a corrected `extract_page` "
                        "tool call. Be careful to satisfy required fields and types."
                    ),
                }
            )

        try:
            message = await client.messages.create(
                model=settings.vision_model,
                # Dense pages (e.g. S0.1 Structural Notes with 5 schedules,
                # 80+ rows) need plenty of room. 16384 is well within Sonnet
                # 4.6's 64k output cap and gives headroom for the densest
                # pages we've seen.
                max_tokens=16384,
                system=SYSTEM_PROMPT,
                tools=[_EXTRACT_TOOL],
                tool_choice={"type": "tool", "name": "extract_page"},
                messages=[{"role": "user", "content": user_blocks}],
            )
        except Exception as e:  # noqa: BLE001
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log.warning(
                "vision: API error on page %d attempt %d (%dms): %s",
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
        usage = usage_from_anthropic(message)
        tool_input = _extract_tool_use_block(message)
        if tool_input is None:
            last_error = RuntimeError(
                f"vision: no extract_page tool_use block in response (stop_reason="
                f"{getattr(message, 'stop_reason', None)})"
            )
            if attempt < settings.vision_max_retries:
                continue
            raise last_error

        try:
            extraction = PageExtractionIn.model_validate(tool_input)
        except ValidationError as e:
            last_error = e
            log.warning(
                "vision: schema validation failed on page %d attempt %d: %s",
                page_number,
                attempt + 1,
                e,
            )
            if attempt < settings.vision_max_retries:
                continue
            raise

        return VisionResult(
            extraction=extraction,
            raw_response=tool_input,
            usage=usage,
            latency_ms=latency_ms,
            model=settings.vision_model,
        )

    # Defensive — loop should always either return or raise above.
    assert last_error is not None
    raise last_error
