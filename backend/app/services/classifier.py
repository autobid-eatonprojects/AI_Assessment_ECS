"""Document classification using Claude Haiku 4.5.

Each uploaded file is classified into one of these types so the rest of the
pipeline can route it to the correct extractor. The taxonomy is intentionally
project-agnostic — it works for any construction project regardless of
program, jurisdiction, or document conventions.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import settings

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)


# Project-side document types (what the GC uploads in the 'setup' phase)
PROJECT_DOC_TYPES = [
    "drawing-set",       # architectural / structural / MEP drawings
    "written-spec",      # CSI Project Manual / written specifications
    "trade-list",        # CSI MasterFormat trade list (Excel/CSV)
    "other",
]

# Bid-side document types (what comes in once project is open-for-bids)
BID_DOC_TYPES = [
    "bid-quote",         # vendor quote, bid, proposal with pricing
    "scope-letter",      # scope summary letter
    "license-insurance", # business license, insurance certificate
    "safety-manual",     # subcontractor HSE manual / training
    "contractor-info",   # contractor questionnaire / qualification application
    "other",
]

# Union for backwards compatibility — the broader taxonomy still surfaces
# for any caller that wants the union (e.g. older indexed data).
DOC_TYPES = sorted(set(PROJECT_DOC_TYPES) | set(BID_DOC_TYPES))


def _classify_tool(allowed_types: list[str], side_hint: str) -> dict:
    return {
        "name": "classify_document",
        "description": (
            "Classify a construction project document into one of the allowed types. "
            f"{side_hint} "
            "Use file metadata (page count, dimensions), visual layout, and any "
            "extracted text. A 200+ page letter-size PDF is almost always a "
            "written-spec (CSI Project Manual), not a drawing-set. "
            "Drawing sets use large-format sheets (24x36, 30x42) with line art, "
            "schedules, and dimension callouts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": allowed_types,
                    "description": "The single best matching type.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "reasoning": {
                    "type": "string",
                    "description": "One short sentence explaining the choice.",
                },
            },
            "required": ["doc_type", "confidence", "reasoning"],
        },
    }


_PROJECT_DOC_TOOL = _classify_tool(
    PROJECT_DOC_TYPES,
    side_hint=(
        "This document was uploaded as a PROJECT DOCUMENT during project setup, "
        "so it should be one of: 'drawing-set', 'written-spec', 'trade-list', or 'other'."
    ),
)
_BID_DOC_TOOL = _classify_tool(
    BID_DOC_TYPES,
    side_hint=(
        "This document was uploaded as a BID SUBMISSION from a subcontractor, "
        "so it should be one of: 'bid-quote', 'scope-letter', 'license-insurance', "
        "'safety-manual', 'contractor-info', or 'other'."
    ),
)


def _tool_for_source(source: str) -> dict:
    if source == "bid_submission":
        return _BID_DOC_TOOL
    return _PROJECT_DOC_TOOL


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


# Anthropic caps the total request body at 32 MB. Base64 inflates raw bytes
# by ~33%, so we cap raw PDFs we send directly at 22 MB (→ ~29 MB base64,
# leaving headroom for the prompt + tool definition).
_PDF_DIRECT_SIZE_LIMIT = 22 * 1024 * 1024


def _extract_pdf_text_sample(path: Path, max_chars: int = 12_000) -> str:
    """Extract the first N chars of text from a PDF using PyMuPDF.

    For large project manuals (CSI specs), the cover sheet + TOC + first few
    spec sections are plenty to classify accurately.
    """
    try:
        import fitz  # PyMuPDF

        text_parts: list[str] = []
        running = 0
        with fitz.open(path) as pdf:
            for page in pdf:
                t = page.get_text("text") or ""
                text_parts.append(t)
                running += len(t)
                if running >= max_chars:
                    break
        return "".join(text_parts)[:max_chars]
    except Exception as e:  # noqa: BLE001
        log.warning("classifier: text extraction failed for %s: %s", path.name, e)
        return ""


def _render_pdf_sample_pages(path: Path, page_indices: list[int]) -> list[bytes]:
    """Render selected pages as small JPEGs for the classifier.

    Used when the PDF is too large to send whole AND/OR has no extractable
    text (scanned). Each rendered page is ~150 KB JPEG, plenty of detail
    for Haiku to recognize a CSI section header or a drawing title block.
    """
    import io

    import fitz  # PyMuPDF
    from PIL import Image

    out: list[bytes] = []
    try:
        with fitz.open(path) as pdf:
            for i in page_indices:
                if i < 0 or i >= len(pdf):
                    continue
                pix = pdf[i].get_pixmap(dpi=110, alpha=False)
                png_bytes = pix.tobytes("png")
                # Re-encode as JPEG quality 80 to keep payload small
                img = Image.open(io.BytesIO(png_bytes))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                # Cap longest edge ~1200 px
                if max(img.size) > 1200:
                    img.thumbnail((1200, 1200), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80, optimize=True)
                out.append(buf.getvalue())
    except Exception as e:  # noqa: BLE001
        log.warning("classifier: page render failed for %s: %s", path.name, e)
    return out


def _pdf_metadata(path: Path) -> tuple[int, tuple[int, int]]:
    """Return (page_count, (page_w_pt, page_h_pt)) — for the metadata header."""
    try:
        import fitz

        with fitz.open(path) as pdf:
            n = len(pdf)
            r = pdf[0].rect if n > 0 else None
            if r is not None:
                return n, (int(r.width), int(r.height))
            return n, (0, 0)
    except Exception:  # noqa: BLE001
        return 0, (0, 0)


def _build_content_for_large_pdf(path: Path) -> list[dict]:
    """Fallback for PDFs over the API size limit OR with no native text.

    Strategy:
      1. Pull first ~12 KB of extractable text (helps if any digital text exists).
      2. Render pages 1, ~middle, ~3/4 as small JPEGs — works for scanned docs.
      3. Include page count + page dimensions so the model can use them as
         a strong feature (200+ letter pages → spec, not drawings).
    """
    text_sample = _extract_pdf_text_sample(path).strip()
    size_mb = path.stat().st_size / (1024 * 1024)
    page_count, (w_pt, h_pt) = _pdf_metadata(path)
    in_w, in_h = w_pt / 72.0, h_pt / 72.0

    # Pick sample pages: first, ~middle, ~3/4 if available.
    sample_idxs: list[int] = [0]
    if page_count >= 4:
        sample_idxs.append(page_count // 2)
    if page_count >= 8:
        sample_idxs.append(int(page_count * 0.75))
    page_images = _render_pdf_sample_pages(path, sample_idxs)

    parts: list[dict] = []
    for idx, img_bytes in zip(sample_idxs, page_images, strict=False):
        b64 = base64.standard_b64encode(img_bytes).decode("ascii")
        parts.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            }
        )
        parts.append(
            {"type": "text", "text": f"(image above is page {idx + 1} of the PDF)"}
        )

    metadata_blurb = (
        f"PDF metadata:\n"
        f"  - file size: {size_mb:.1f} MB\n"
        f"  - page count: {page_count}\n"
        f"  - page size: {in_w:.1f} × {in_h:.1f} inches "
        f"(letter ~ 8.5×11, drawing sheets typically 24×36 or 30×42)\n"
        f"  - has extractable text: {bool(text_sample)}\n"
    )

    if text_sample:
        text_blurb = (
            "First ~12 KB of extracted text:\n---\n" + text_sample + "\n---\n"
        )
    else:
        text_blurb = (
            "No extractable text — this is a scanned/image-only PDF. "
            "Use the rendered sample pages above to classify it.\n"
        )

    parts.append(
        {
            "type": "text",
            "text": metadata_blurb + "\n" + text_blurb + "\nUse the classify_document tool.",
        }
    )
    return parts


def _build_content_for_pdf(path: Path) -> list[dict]:
    """Send the PDF directly when small; fall back to text-only when too large."""
    if path.stat().st_size > _PDF_DIRECT_SIZE_LIMIT:
        log.info(
            "classifier: %s exceeds %d MB; using text-extraction fallback",
            path.name,
            _PDF_DIRECT_SIZE_LIMIT // (1024 * 1024),
        )
        return _build_content_for_large_pdf(path)

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


def _is_spreadsheet(path: Path) -> bool:
    return path.suffix.lower() in {".xlsx", ".xlsm", ".xls", ".csv"}


def _spreadsheet_csi_heuristic(path: Path) -> Classification | None:
    """Quick rule-based check: an .xlsx that looks like a CSI MasterFormat
    list is unambiguously `trade-list`. Saves an LLM call and is more
    reliable than feeding garbled binary text to Claude."""
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return None
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        sheet = wb.active
        # Sample first 30 rows looking for the CSI pattern: rows where col 0
        # is a 6-digit-formatted code "NN NN NN".
        section_re = re.compile(r"^\s*\d{2}\s+\d{2}\s+\d{2}\s*$")
        section_hits = 0
        sampled = 0
        for row in sheet.iter_rows(max_row=50, values_only=True):
            sampled += 1
            if not row:
                continue
            cell = row[0] if row else None
            if cell and section_re.match(str(cell)):
                section_hits += 1
        wb.close()
        if section_hits >= 5:
            return Classification(
                doc_type="trade-list",
                confidence=0.99,
                reasoning=(
                    f"Spreadsheet contains {section_hits}+ rows matching the "
                    "CSI MasterFormat 'NN NN NN' section-code format."
                ),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("classifier: xlsx heuristic failed for %s: %s", path.name, e)
    return None


def _build_content_for_spreadsheet(path: Path) -> list[dict]:
    """Sample rows from an xlsx and send the text to Claude."""
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        sheet = wb.active
        rows = []
        for i, row in enumerate(sheet.iter_rows(max_row=20, values_only=True)):
            row_str = " | ".join(str(c) if c is not None else "" for c in row[:8])
            rows.append(f"row {i + 1}: {row_str}")
        wb.close()
        sample = "\n".join(rows)
    except Exception as e:  # noqa: BLE001
        sample = f"(could not read xlsx: {e})"
    return [
        {
            "type": "text",
            "text": (
                f"This is an Excel spreadsheet (.xlsx). First 20 rows:\n"
                f"---\n{sample}\n---\n"
                "Use the classify_document tool. If it looks like a CSI "
                "MasterFormat trade list, mark as 'trade-list'."
            ),
        }
    ]


async def classify(
    path: Path, content_type: str, *, source: str = "project_document"
) -> Classification:
    """Classify a single document. Raises ClassifierUnavailable if no API key.

    `source` selects between the project-doc and bid-doc taxonomies — the
    classifier only chooses among the doc_types valid for that side.
    """
    # Fast path: spreadsheet with CSI structure → trade-list, no LLM needed.
    if source == "project_document":
        heuristic = _spreadsheet_csi_heuristic(path)
        if heuristic is not None:
            log.info(
                "classifier: %s matched CSI heuristic → %s",
                path.name,
                heuristic.doc_type,
            )
            return heuristic

    client = _get_client()

    if _is_pdf(path):
        content = _build_content_for_pdf(path)
    elif _is_image(content_type, path):
        content = _build_content_for_image(path)
    elif _is_spreadsheet(path):
        content = _build_content_for_spreadsheet(path)
    else:
        content = _build_content_for_text(path, content_type)

    tool = _tool_for_source(source)
    log.info(
        "classifying %s (%s, source=%s) with %s",
        path.name,
        content_type,
        source,
        settings.classifier_model,
    )

    response = await client.messages.create(
        model=settings.classifier_model,
        max_tokens=512,
        tools=[tool],
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


async def classify_with_retry(
    path: Path,
    content_type: str,
    *,
    source: str = "project_document",
    retries: int = 2,
) -> Classification:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await classify(path, content_type, source=source)
        except ClassifierUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("classify attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(min(2**attempt, 5))
    assert last is not None
    raise last
