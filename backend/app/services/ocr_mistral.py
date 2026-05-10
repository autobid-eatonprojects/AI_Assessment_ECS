"""Mistral OCR provider — primary OCR per the design doc P1.

Mistral OCR is the published best-in-class for AEC manual layout (Division
→ Section → Article hierarchy preserved). API:
    POST https://api.mistral.ai/v1/ocr
    headers: Authorization: Bearer <key>
    body: {model, document: {type: image_url, image_url: <data:.../base64>}}

Returns layout-aware Markdown by page. We collapse to plain text (matching
the existing Gemini OCR provider's contract) so the chunker doesn't care
who produced the OCR.

Setup:
    Add MISTRAL_API_KEY to backend/.env. The triple-OCR voter (ocr.py)
    detects the key and enables this provider automatically.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path

import httpx

from ..config import settings
from .llm_log import Usage
from .ocr_types import OCRResult

log = logging.getLogger(__name__)


_MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"
_MISTRAL_MODEL = "mistral-ocr-latest"
_TIMEOUT = 60.0
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3

_concurrency_sem: asyncio.Semaphore | None = None


class MistralOCRUnavailable(Exception):
    """Raised when MISTRAL_API_KEY is not configured."""


def _get_semaphore() -> asyncio.Semaphore:
    global _concurrency_sem
    if _concurrency_sem is None:
        _concurrency_sem = asyncio.Semaphore(4)
    return _concurrency_sem


def is_available() -> bool:
    return bool(getattr(settings, "mistral_api_key", None))


async def ocr_image(image_path: Path) -> OCRResult:
    """OCR one rendered page image with Mistral OCR."""
    api_key = getattr(settings, "mistral_api_key", None)
    if not api_key:
        raise MistralOCRUnavailable("MISTRAL_API_KEY is not set")

    raw = image_path.read_bytes()
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    data_url = f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"

    payload = {
        "model": _MISTRAL_MODEL,
        "document": {
            "type": "image_url",
            "image_url": data_url,
        },
        # Mistral returns layout-aware Markdown. We collapse to text below.
        "include_image_base64": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    sem = _get_semaphore()
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        async with sem:
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        _MISTRAL_OCR_URL, json=payload, headers=headers
                    )
                if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    delay = 2**attempt
                    log.info(
                        "mistral ocr: %d on attempt %d, retrying in %ds",
                        resp.status_code, attempt + 1, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
            latency_ms = int((time.perf_counter() - t0) * 1000)

        # Response shape: {"pages": [{"index", "markdown", ...}], "usage_info": ...}
        pages = data.get("pages") or []
        markdown_parts = [p.get("markdown") or "" for p in pages]
        text = "\n\n".join(markdown_parts).strip()

        usage_info = data.get("usage_info") or {}
        usage = Usage(
            prompt_tokens=int(usage_info.get("input_tokens") or 0),
            completion_tokens=int(usage_info.get("output_tokens") or 0),
        )
        return OCRResult(
            text=text,
            usage=usage,
            latency_ms=latency_ms,
            provider="mistral",
        )

    assert last_exc is not None
    raise last_exc
