"""Document classification using Claude Haiku 4.5.

Each uploaded file is classified into one of these types so the rest of the
pipeline can route it to the correct extractor. The taxonomy is intentionally
project-agnostic — it works for any construction project, not just the Elks
sample.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import settings

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)


DOC_TYPES = [
    "drawing-set",       # architectural / structural / MEP drawings
    "written-spec",      # CSI written specifications
    "bid-quote",         # vendor quote, bid, proposal with pricing
    "scope-letter",      # scope summary letter (no pricing)
    "license-insurance", # business license, insurance certificate, registration
    "safety-manual",     # health / safety / environmental docs
    "contractor-info",   # contractor questionnaire / qualification application
    "other",
]

_CLASSIFY_TOOL = {
    "name": "classify_document",
    "description": (
        "Classify a construction project document into one of the canonical types. "
        "Use the visual layout, headers, and any visible text to decide. "
        "Be conservative: if it's a drawing sheet (large-format with line art / "
        "schedules / dimension callouts), choose 'drawing-set'. If it has a price "
        "table or 'Quote' / 'Proposal' header, choose 'bid-quote'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_type": {
                "type": "string",
                "enum": DOC_TYPES,
                "description": "The single best matching type.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "Calibrated confidence in [0, 1]. "
                    "1.0 means certain; 0.5 means roughly even between two types."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence explaining what features drove the choice.",
            },
        },
        "required": ["doc_type", "confidence", "reasoning"],
    },
}


@dataclass
class Classification:
    doc_type: str
    confidence: float
    reasoning: str


class ClassifierUnavailable(Exception):
    """Raised when no API key is configured."""


_client: "AsyncAnthropic | None" = None


def _get_client() -> "AsyncAnthropic":
    global _client
    if _client is not None:
        return _client
    if not settings.anthropic_api_key:
        raise ClassifierUnavailable("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def _is_image(content_type: str, path: Path) -> bool:
    if content_type.startswith("image/"):
        return True
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _image_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")


def _build_content_for_pdf(path: Path) -> list[dict]:
    """Send the PDF directly via Anthropic's native PDF support."""
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": data,
            },
        },
        {
            "type": "text",
            "text": (
                "Classify this construction project document. Look at layout, "
                "headers, and any visible content. Use the classify_document tool."
            ),
        },
    ]


def _build_content_for_image(path: Path) -> list[dict]:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _image_media_type(path),
                "data": data,
            },
        },
        {
            "type": "text",
            "text": (
                "Classify this construction project document. "
                "Use the classify_document tool."
            ),
        },
    ]


def _build_content_for_text(path: Path, content_type: str) -> list[dict]:
    """Fallback: read up to 10KB of text and classify by content."""
    try:
        sample = path.read_text(errors="replace")[:10_000]
    except Exception:
        sample = f"(unreadable file, content_type={content_type})"
    return [
        {
            "type": "text",
            "text": (
                f"Classify the following construction document content "
                f"(content_type={content_type}). "
                f"Use the classify_document tool.\n\n---\n{sample}\n---"
            ),
        }
    ]


async def classify(path: Path, content_type: str) -> Classification:
    """Classify a single document. Raises ClassifierUnavailable if no API key."""
    client = _get_client()

    if _is_pdf(path):
        content = _build_content_for_pdf(path)
    elif _is_image(content_type, path):
        content = _build_content_for_image(path)
    else:
        content = _build_content_for_text(path, content_type)

    log.info("classifying %s (%s) with %s", path.name, content_type, settings.classifier_model)

    response = await client.messages.create(
        model=settings.classifier_model,
        max_tokens=512,
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_document"},
        messages=[{"role": "user", "content": content}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "classify_document":
            data = block.input
            return Classification(
                doc_type=data["doc_type"],
                confidence=float(data["confidence"]),
                reasoning=data["reasoning"],
            )

    raise RuntimeError("classifier returned no tool_use block")


async def classify_with_retry(path: Path, content_type: str, retries: int = 2) -> Classification:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await classify(path, content_type)
        except ClassifierUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("classify attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(min(2**attempt, 5))
    assert last is not None
    raise last
