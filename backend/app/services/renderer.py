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

            rendered.append(
                RenderedPage(
                    page_number=i,
                    width=pix.width,
                    height=pix.height,
                    image_path=image_path,
                    thumbnail_path=thumb_path,
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


async def render(source: Path, document_id: str) -> list[RenderedPage]:
    log.info("rendering pages for %s (%s)", document_id, source.name)
    return await asyncio.to_thread(render_sync, source, document_id)
