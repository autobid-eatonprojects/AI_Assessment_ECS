"""PDF page rendering: full-resolution PNG + thumbnail per page.

PyMuPDF is fast and self-contained (no Poppler / Ghostscript required).
Single-image documents (PNG / JPG) are also rendered to a single page entry
so the rest of the pipeline can treat all documents uniformly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from ..config import settings

log = logging.getLogger(__name__)


@dataclass
class RenderedPage:
    page_number: int  # 1-indexed
    width: int
    height: int
    image_path: Path
    thumbnail_path: Path
    # Native PyMuPDF text. Empty string for scanned PDFs / images;
    # OCR fills it in later when needed.
    native_text: str = ""


def _pages_dir(document_id: str) -> Path:
    d = settings.storage_root / "_pages" / document_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_thumbnail(image_bytes: bytes, out_path: Path) -> None:
    img = Image.open(BytesIO(image_bytes))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    img.thumbnail((settings.thumbnail_max_dim, settings.thumbnail_max_dim), Image.LANCZOS)
    img.save(out_path, format="PNG", optimize=True)


def _render_pdf_sync(source: Path, document_id: str) -> list[RenderedPage]:
    out_dir = _pages_dir(document_id)
    rendered: list[RenderedPage] = []

    with fitz.open(source) as pdf:
        zoom = settings.page_dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(pdf, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            png_bytes = pix.tobytes("png")

            image_path = out_dir / f"p{i:04d}.png"
            image_path.write_bytes(png_bytes)

            thumb_path = out_dir / f"p{i:04d}_thumb.png"
            _make_thumbnail(png_bytes, thumb_path)

            # Pull native text in the same pass — free where it exists.
            native = page.get_text("text") or ""

            rendered.append(
                RenderedPage(
                    page_number=i,
                    width=pix.width,
                    height=pix.height,
                    image_path=image_path,
                    thumbnail_path=thumb_path,
                    native_text=native,
                )
            )

    return rendered


def _render_image_sync(source: Path, document_id: str) -> list[RenderedPage]:
    out_dir = _pages_dir(document_id)
    img = Image.open(source)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    image_path = out_dir / "p0001.png"
    img.save(image_path, format="PNG", optimize=True)

    thumb_path = out_dir / "p0001_thumb.png"
    thumb = img.copy()
    thumb.thumbnail((settings.thumbnail_max_dim, settings.thumbnail_max_dim), Image.LANCZOS)
    thumb.save(thumb_path, format="PNG", optimize=True)

    return [
        RenderedPage(
            page_number=1,
            width=img.width,
            height=img.height,
            image_path=image_path,
            thumbnail_path=thumb_path,
        )
    ]


def _is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def render_sync(source: Path, document_id: str) -> list[RenderedPage]:
    if _is_pdf(source):
        return _render_pdf_sync(source, document_id)
    if _is_image(source):
        return _render_image_sync(source, document_id)
    return []  # unsupported types yield no pages


# Rendering is CPU-bound and PyMuPDF doesn't reliably release the GIL, so two
# concurrent `asyncio.to_thread` calls can stall the event loop and starve
# other tasks (e.g. an in-flight Anthropic classification). A global semaphore
# of 1 turns the renderer into a queue: predictable throughput without GIL
# contention. Phase 2 will swap this for a process pool when we add the heavy
# vision pre-pass.
_render_semaphore = asyncio.Semaphore(1)


async def render(source: Path, document_id: str) -> list[RenderedPage]:
    async with _render_semaphore:
        log.info("rendering pages for %s (%s)", document_id, source.name)
        return await asyncio.to_thread(render_sync, source, document_id)


# -----------------------------------------------------------------------------
# DPI tiering — per the design doc P1, render at the right resolution for the
# downstream consumer:
#   - 150 DPI: routing thumbnails (page_summary classifier, schedule router)
#   - 300 DPI: vision passes (Sonnet on full-page schedule/notes/entities)
#   - 600 DPI: tile crops on schedule/detail regions (per-schedule-type schemas)
#
# `render_at_dpi` re-renders one page on demand and caches by (doc, page, dpi).
# Vision pre-pass calls this with dpi=300 instead of using the 150 DPI thumb.
# -----------------------------------------------------------------------------


def _hi_res_path(document_id: str, page_number: int, dpi: int) -> Path:
    return _pages_dir(document_id) / f"p{page_number:04d}_{dpi}dpi.png"


# Anthropic's vision API rejects images >8000 px in either dimension.
# We clamp our high-DPI renders so a large arch-D sheet at 300 DPI doesn't
# blow past the limit. Vision providers (Gemini, OpenAI) have similar
# upper bounds; 8000 is the strictest so it's the universal cap.
_MAX_VISION_DIM_PX = 8000


def _effective_dpi(page, requested_dpi: int) -> int:
    """Clamp the requested DPI down so neither dimension exceeds the
    vision API ceiling. Returns the highest DPI that fits.
    """
    rect = page.rect  # in PDF points (1/72")
    page_w_in = rect.width / 72.0
    page_h_in = rect.height / 72.0
    if page_w_in <= 0 or page_h_in <= 0:
        return requested_dpi
    max_dpi_w = int(_MAX_VISION_DIM_PX // page_w_in)
    max_dpi_h = int(_MAX_VISION_DIM_PX // page_h_in)
    return min(requested_dpi, max_dpi_w, max_dpi_h)


def render_page_at_dpi_sync(
    source: Path, document_id: str, page_number: int, dpi: int
) -> Path:
    """Render one PDF page at the requested DPI. Returns the on-disk PNG path.

    The actual render DPI is clamped so neither dimension exceeds 8000 px
    (vision API ceiling). The output filename records the *effective* DPI,
    so subsequent calls with the same `dpi` argument cache-hit correctly.

    Idempotent: if the file already exists, returns the path without
    re-rendering (the doc + page + dpi tuple is the cache key).
    """
    if not _is_pdf(source):
        # For single-image docs, the original render is the only render
        return _pages_dir(document_id) / f"p{page_number:04d}.png"

    with fitz.open(source) as pdf:
        if page_number < 1 or page_number > len(pdf):
            raise ValueError(
                f"page {page_number} out of range (doc has {len(pdf)} pages)"
            )
        page = pdf[page_number - 1]
        eff_dpi = _effective_dpi(page, dpi)
        out_path = _hi_res_path(document_id, page_number, eff_dpi)
        if out_path.exists():
            return out_path
        if eff_dpi != dpi:
            log.info(
                "render: clamping page %d DPI %d → %d (vision dim cap)",
                page_number, dpi, eff_dpi,
            )
        zoom = eff_dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out_path.write_bytes(pix.tobytes("png"))
    return out_path


async def render_page_at_dpi(
    source: Path, document_id: str, page_number: int, dpi: int = 300
) -> Path:
    """Async wrapper. Used by vision_extractor + per-schedule-type extractors."""
    async with _render_semaphore:
        return await asyncio.to_thread(
            render_page_at_dpi_sync, source, document_id, page_number, dpi
        )


def crop_region_sync(
    source: Path,
    document_id: str,
    page_number: int,
    bbox: tuple[float, float, float, float],
    dpi: int = 600,
) -> Path:
    """Render a high-DPI tile crop of one bbox region on a page.

    bbox is normalised [0,1] in the same coord system as our extractions:
    (x0, y0, x1, y1) with (0,0) top-left, (1,1) bottom-right.

    Used by per-schedule-type extractors (P2) to give the model maximum
    grid-line resolution on the table without paying for the full page.

    DPI is clamped down so neither dimension exceeds the 8000-px vision
    API ceiling. For a half-page crop on an arch-D sheet the requested
    600 DPI fits; for a near-full-page bbox we drop to ~300 DPI.
    """
    x0, y0, x1, y1 = bbox
    if not _is_pdf(source):
        raise ValueError("crop_region only supported on PDF sources")
    with fitz.open(source) as pdf:
        page = pdf[page_number - 1]
        rect = page.rect
        crop_w_in = (x1 - x0) * rect.width / 72.0
        crop_h_in = (y1 - y0) * rect.height / 72.0
        if crop_w_in <= 0 or crop_h_in <= 0:
            raise ValueError(f"degenerate bbox {bbox}")
        # Clamp to keep both crop dimensions ≤ 8000 px
        max_dpi_w = int(_MAX_VISION_DIM_PX // crop_w_in)
        max_dpi_h = int(_MAX_VISION_DIM_PX // crop_h_in)
        eff_dpi = max(72, min(dpi, max_dpi_w, max_dpi_h))

        out_dir = _pages_dir(document_id)
        crop_path = out_dir / (
            f"p{page_number:04d}_{eff_dpi}dpi_"
            f"crop_{int(x0*1000)}_{int(y0*1000)}_{int(x1*1000)}_{int(y1*1000)}.png"
        )
        if crop_path.exists():
            return crop_path
        if eff_dpi != dpi:
            log.info(
                "crop_region: clamping page %d DPI %d → %d (crop %.1fx%.1f in)",
                page_number, dpi, eff_dpi, crop_w_in, crop_h_in,
            )
        clip = fitz.Rect(
            rect.x0 + x0 * rect.width,
            rect.y0 + y0 * rect.height,
            rect.x0 + x1 * rect.width,
            rect.y0 + y1 * rect.height,
        )
        zoom = eff_dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        crop_path.write_bytes(pix.tobytes("png"))
    return crop_path


async def crop_region(
    source: Path,
    document_id: str,
    page_number: int,
    bbox: tuple[float, float, float, float],
    dpi: int = 600,
) -> Path:
    async with _render_semaphore:
        return await asyncio.to_thread(
            crop_region_sync, source, document_id, page_number, bbox, dpi
        )


# -----------------------------------------------------------------------------
# Helper for vision API consumers: read an image file and return bytes that
# fit Anthropic's 5MB vision payload limit. Downsamples to JPEG quality 85
# if the source PNG is too large. Caps dim at 4000 px as extra safety.
# -----------------------------------------------------------------------------


def read_image_for_vision(
    image_path: Path, *, max_bytes: int = 5 * 1024 * 1024, max_dim: int = 4000
) -> tuple[bytes, str]:
    """Returns (bytes, mime_type) ready to send to a vision API."""
    raw = image_path.read_bytes()
    if len(raw) <= max_bytes:
        media = (
            "image/png"
            if image_path.suffix.lower() == ".png"
            else "image/jpeg"
        )
        return raw, media
    from io import BytesIO
    from PIL import Image as PILImage

    img = PILImage.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), PILImage.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    out = buf.getvalue()
    log.info(
        "vision: downsized %s %dKB → %dKB JPEG (Anthropic 5MB cap)",
        image_path.name, len(raw) // 1024, len(out) // 1024,
    )
    return out, "image/jpeg"
