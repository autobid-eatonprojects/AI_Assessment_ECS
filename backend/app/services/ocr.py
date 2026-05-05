"""OCR for scanned PDFs (project manuals especially).

When a `written-spec` document arrives without a native text layer, we use
Gemini 2.5 Flash to OCR each rendered page image. Output is plain text per
page, written into `DocumentPage.text_content` so the existing chunker
indexes it transparently.

Why Gemini Flash:
  - Fast (~3-5s per page)
  - Cheap (~$0.001 per page at 110 DPI)
  - Strong on multi-column technical text and CSI section formatting
  - We already have google-genai wired up

The classifier and Phase 2 vision-extractor still use Sonnet/Pro for richer
reasoning; Gemini Flash here is purpose-fit for plain-text OCR.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .llm_log import Usage

log = logging.getLogger(__name__)


_OCR_MODEL = "gemini-2.5-flash"
_OCR_PROMPT = """\
You are an OCR engine. Extract ALL visible text from the page image, preserving:
- Reading order (top to bottom, left to right; multi-column layouts read as
  full left column then full right column).
- Section headers (use ALL CAPS exactly as printed, e.g. "PART 1 — GENERAL",
  "SECTION 03 30 00 — CAST-IN-PLACE CONCRETE").
- Numbered/lettered lists with their indentation cues (1., A., a., etc.).
- Footers with CSI section codes (e.g. "03 30 00.4").
Return ONLY the extracted text — no commentary, no formatting markers.
Do not summarise; transcribe verbatim.
"""


_client = None
_concurrency_sem: asyncio.Semaphore | None = None


class OCRUnavailable(Exception):
    """Raised when Google API key is not configured."""


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.google_api_key:
        raise OCRUnavailable("GOOGLE_API_KEY is not set")
    from google import genai

    _client = genai.Client(api_key=settings.google_api_key)
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _concurrency_sem
    if _concurrency_sem is None:
        # OCR is light enough to run more in parallel than vision pre-pass.
        _concurrency_sem = asyncio.Semaphore(max(8, settings.vision_concurrency))
    return _concurrency_sem


@dataclass
class OCRResult:
    text: str
    usage: Usage
    latency_ms: int


async def ocr_image(image_path: Path) -> OCRResult:
    """OCR a single rendered page image using Gemini Flash."""
    client = _get_client()
    sem = _get_semaphore()

    raw = image_path.read_bytes()
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    from google.genai import types as gtypes

    content = [
        gtypes.Part.from_bytes(data=raw, mime_type=media_type),
        gtypes.Part.from_text(text=_OCR_PROMPT),
    ]

    async with sem:
        t0 = time.perf_counter()
        resp = await client.aio.models.generate_content(
            model=_OCR_MODEL,
            contents=content,
            config=gtypes.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=8192,
            ),
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

    usage_meta = getattr(resp, "usage_metadata", None)
    usage = Usage(
        prompt_tokens=getattr(usage_meta, "prompt_token_count", 0) or 0,
        completion_tokens=getattr(usage_meta, "candidates_token_count", 0) or 0,
    )
    text = (getattr(resp, "text", "") or "").strip()
    return OCRResult(text=text, usage=usage, latency_ms=latency_ms)
