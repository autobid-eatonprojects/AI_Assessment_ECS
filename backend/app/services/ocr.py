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
        # Gemini Flash starts returning 503s above ~5 concurrent requests for
        # us. We pair this with retry-with-backoff so brief spikes recover
        # instead of producing silent failures.
        _concurrency_sem = asyncio.Semaphore(4)
    return _concurrency_sem


@dataclass
class OCRResult:
    text: str
    usage: Usage
    latency_ms: int


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5


def _is_retryable(exc: Exception) -> bool:
    """Spot Gemini's transient errors (rate limit / overload / 5xx)."""
    msg = str(exc).lower()
    if "503" in msg or "unavailable" in msg or "high demand" in msg:
        return True
    if "429" in msg or "rate limit" in msg or "resource_exhausted" in msg:
        return True
    if "500" in msg or "502" in msg or "504" in msg:
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _RETRYABLE_STATUS:
        return True
    return False


async def ocr_image(image_path: Path) -> OCRResult:
    """OCR a single rendered page image using Gemini Flash, with retry-on-overload."""
    client = _get_client()
    sem = _get_semaphore()

    raw = image_path.read_bytes()
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    from google.genai import types as gtypes

    content = [
        gtypes.Part.from_bytes(data=raw, mime_type=media_type),
        gtypes.Part.from_text(text=_OCR_PROMPT),
    ]

    last: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        async with sem:
            t0 = time.perf_counter()
            try:
                resp = await client.aio.models.generate_content(
                    model=_OCR_MODEL,
                    contents=content,
                    config=gtypes.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=8192,
                    ),
                )
            except Exception as e:  # noqa: BLE001
                last = e
                if not _is_retryable(e) or attempt == _MAX_RETRIES:
                    raise
                # Exponential backoff with jitter: 1s, 2s, 4s, 8s, 16s
                import random

                delay = (2**attempt) + random.uniform(0, 0.5)
                log.info(
                    "ocr: retry %d after %.1fs (transient error: %s)",
                    attempt + 1,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
                continue
            latency_ms = int((time.perf_counter() - t0) * 1000)

        usage_meta = getattr(resp, "usage_metadata", None)
        usage = Usage(
            prompt_tokens=getattr(usage_meta, "prompt_token_count", 0) or 0,
            completion_tokens=getattr(usage_meta, "candidates_token_count", 0) or 0,
        )
        text = (getattr(resp, "text", "") or "").strip()
        return OCRResult(text=text, usage=usage, latency_ms=latency_ms)

    assert last is not None
    raise last
