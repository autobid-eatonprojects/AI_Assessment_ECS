"""A/B follow-up: tiled vision vs. whole-sheet vision.

For each test sheet:
  - Re-render the source PDF page at 300 DPI (vs the project's 150)
  - Split into 4 quadrants (TL, TR, BL, BR)
  - Run a per-quadrant vision query with sub-image-aware prompt
  - Aggregate findings across the 4 tiles

Compare against the whole-sheet result captured earlier.

Run:
  cd backend
  .venv/bin/python -m scripts.ab_tiled_vision
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Document, PageExtraction


_TEST_CASES = [
    {
        "label": "Floor outlet",
        "sheet": "E3.1",
        "what": "FLOOR OUTLETS — in-floor electrical receptacle boxes",
        "context": (
            "Sheet E3.1 — POWER PLAN of a community center. Floor outlets are "
            "shown with circle-with-cross or in-floor-box symbols (per the "
            "legend on E0.1). Distinguish from wall outlets (which are at the "
            "perimeter)."
        ),
    },
    {
        "label": "Condensate drain",
        "sheet": "M1.1",
        "what": "CONDENSATE DRAIN LINES from HVAC equipment",
        "context": (
            "Sheet M1.1 — HVAC PLAN. Condensate drain lines route from AHU/HP/"
            "RTU equipment to nearest receptacle or floor drain. Line type is "
            "typically a thin solid line tagged 'CD' or shown as a thin line "
            "from a piece of equipment to a drain symbol."
        ),
    },
    {
        "label": "WC1 Water Closet",
        "sheet": "P2.1",
        "what": "WC1 fixtures (Water Closet, standard height)",
        "context": (
            "Sheet P2.1 — DOMESTIC WATER PLAN. WC1 fixtures are tagged 'WC1' "
            "with a leader line. They are toilets — should appear in restroom "
            "rooms with cold water + waste connections."
        ),
    },
]


@dataclass
class TileResult:
    quadrant: str   # TL, TR, BL, BR
    response: str
    tokens_in: int
    tokens_out: int


def _render_at_dpi(pdf_path: Path, page_number: int, dpi: int) -> bytes:
    """PyMuPDF render of a single page to PNG bytes."""
    with fitz.open(pdf_path) as pdf:
        page = pdf[page_number - 1]
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")


def _split_quadrants(png_bytes: bytes) -> dict[str, bytes]:
    """Split the image into 4 quadrants. Returns {label: png_bytes}."""
    img = Image.open(io.BytesIO(png_bytes))
    w, h = img.size
    crops = {
        "TL": img.crop((0, 0, w // 2, h // 2)),
        "TR": img.crop((w // 2, 0, w, h // 2)),
        "BL": img.crop((0, h // 2, w // 2, h)),
        "BR": img.crop((w // 2, h // 2, w, h)),
    }
    out: dict[str, bytes] = {}
    for label, im in crops.items():
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        out[label] = buf.getvalue()
    return out


async def _ask_one_tile(client, quadrant: str, png_bytes: bytes, case) -> TileResult:
    img_b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
    prompt = (
        f"You are looking at the {quadrant} (top-left=TL, top-right=TR, "
        f"bottom-left=BL, bottom-right=BR) quadrant of a construction "
        f"drawing.\n\n{case['context']}\n\n"
        f"Find every {case['what']} visible IN THIS QUADRANT only.\n\n"
        f"For each one, report:\n"
        f"  - Room name/number where it appears (or 'unlabeled' if outside a room)\n"
        f"  - Approximate location within the quadrant (e.g. 'near top-left', "
        f"'centered', 'along east wall')\n"
        f"  - Symbol/tag used\n"
        f"  - Any callout text\n\n"
        f"Then state the COUNT in this quadrant. Be precise; if zero, say zero."
    )
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": img_b64,
                }},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    text_parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    return TileResult(
        quadrant=quadrant,
        response="\n".join(text_parts),
        tokens_in=msg.usage.input_tokens,
        tokens_out=msg.usage.output_tokens,
    )


async def _ask_tiled(pdf_path: Path, page_number: int, case) -> list[TileResult]:
    """Render at 300 DPI, split into 4, query each in parallel."""
    if not settings.anthropic_api_key:
        return []
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    png = _render_at_dpi(pdf_path, page_number, dpi=300)
    quadrants = _split_quadrants(png)
    tasks = [_ask_one_tile(client, q, b, case) for q, b in quadrants.items()]
    return await asyncio.gather(*tasks)


async def main() -> int:
    async with SessionLocal() as db:
        d = (await db.execute(
            select(Document).where(Document.doc_type == "drawing-set")
            .order_by(Document.created_at.desc()).limit(1)
        )).scalar_one()
        pdf_path = Path("data/uploads") / d.storage_path
        pages = (await db.execute(
            select(PageExtraction.sheet_number, PageExtraction.page_number)
            .where(PageExtraction.document_id == d.id)
        )).all()
        sheet_to_page = {sn: pn for sn, pn in pages}

    total_in = 0
    total_out = 0
    for case in _TEST_CASES:
        sheet = case["sheet"]
        page_number = sheet_to_page.get(sheet)
        print("\n" + "=" * 100)
        print(f"TILED VISION — {case['label']!r}  →  sheet {sheet} (page {page_number}, 300 DPI, 4 quadrants)")
        print("=" * 100)
        if not page_number:
            print(f"(no page for sheet {sheet})")
            continue
        try:
            tiles = await _ask_tiled(pdf_path, page_number, case)
        except Exception as e:
            print(f"  vision call failed: {e}")
            continue
        for t in tiles:
            print(f"\n--- QUADRANT {t.quadrant} ---")
            print(t.response[:1500])
            print(f"[tokens: in={t.tokens_in} out={t.tokens_out}]")
            total_in += t.tokens_in
            total_out += t.tokens_out

    print("\n" + "=" * 100)
    print(f"TOTAL TOKENS: in={total_in:,}  out={total_out:,}  "
          f"(rough cost @Sonnet 4.6 ~$0.{(total_in*3 + total_out*15)//1000:04d})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
