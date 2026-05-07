"""A/B test: discipline_agent's text-flattened drawing evidence vs.
direct Sonnet vision call against the actual sheet image.

For each test item:
  - Print the current drawing evidence (chunks the discipline_agent grabbed)
  - Render-load the relevant layout-plan sheet PNG
  - Send to Sonnet 4.6 vision: "find every <item> on this sheet"
  - Print the vision-grounded answer

Run:
  cd backend
  .venv/bin/python -m scripts.ab_drawing_vision
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import (
    Chunk, Document, DocumentPage, PageExtraction,
    ScopeCitation, ScopeItem,
)


# Test cases: (item description substring, target layout sheet, vision query)
_TEST_CASES = [
    {
        "find": "Floor outlet",
        "sheet": "E3.1",
        "query": (
            "This is sheet E3.1 — POWER PLAN of a community center project. "
            "Find every FLOOR OUTLET shown on this plan. For each, report:\n"
            "  - The room where it appears (room name or number)\n"
            "  - The approximate location in that room\n"
            "  - Total count across the whole plan\n"
            "  - Any specific notes/callouts associated with floor outlets.\n"
            "Only count outlets that are CALLED OUT as floor outlets (in-floor "
            "boxes, often shown with a circle-with-cross or specific symbol "
            "from the legend). Do not include wall outlets."
        ),
    },
    {
        "find": "Condensate drain",
        "sheet": "M1.1",
        "query": (
            "This is sheet M1.1 — HVAC PLAN. Find every condensate drain "
            "line shown on the plan. For each, report:\n"
            "  - Source equipment (which AHU/HP/RTU it serves)\n"
            "  - Routing destination (where it discharges — to receptacle, "
            "    floor drain, etc.)\n"
            "  - Approximate length if measurable\n"
            "  - Total count of condensate drain lines.\n"
            "If no condensate drain lines are visible, say so."
        ),
    },
    {
        "find": "WC1",
        "sheet": "P2.1",
        "query": (
            "This is sheet P2.1 — DOMESTIC WATER PLAN of a community center. "
            "Find every fixture tagged WC1 (Water Closet, standard height). "
            "Report:\n"
            "  - Total WC1 count\n"
            "  - Room locations (which restrooms/spaces)\n"
            "  - Any notes on the symbol or callout."
        ),
    },
]


async def _gather_test_data():
    """Pull current drawing evidence for each test item + the target sheet image path."""
    async with SessionLocal() as db:
        latest = (await db.execute(
            select(ScopeItem.run_id, ScopeItem.project_id)
            .order_by(ScopeItem.created_at.desc()).limit(1)
        )).first()
        run_id = latest[0]

        items = (await db.execute(
            select(ScopeItem).where(ScopeItem.run_id == run_id)
        )).scalars().all()
        cits = (await db.execute(
            select(ScopeCitation).where(
                ScopeCitation.scope_item_id.in_([i.id for i in items])
            )
        )).scalars().all()
        chunks = (await db.execute(
            select(Chunk).where(Chunk.id.in_({c.chunk_id for c in cits}))
        )).scalars().all()
        chunk_by_id = {c.id: c for c in chunks}

        d = (await db.execute(
            select(Document).where(Document.doc_type == "drawing-set")
            .order_by(Document.created_at.desc()).limit(1)
        )).scalar_one()
        pages = (await db.execute(
            select(PageExtraction.sheet_number, PageExtraction.page_number, DocumentPage.image_path)
            .join(DocumentPage, (DocumentPage.document_id == PageExtraction.document_id)
                  & (DocumentPage.page_number == PageExtraction.page_number))
            .where(PageExtraction.document_id == d.id)
        )).all()
        sheet_to_image = {sn: ip for sn, _, ip in pages}

    return items, cits, chunk_by_id, sheet_to_image


async def _vision_query(image_path: Path, prompt: str) -> tuple[str, dict]:
    """One Sonnet 4.6 vision call. Returns (text_response, usage_dict)."""
    if not settings.anthropic_api_key:
        return "(no ANTHROPIC_API_KEY)", {}
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    img_bytes = image_path.read_bytes()
    img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")

    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
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
    return "\n".join(text_parts), {
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
    }


async def main() -> int:
    items, cits, chunk_by_id, sheet_to_image = await _gather_test_data()
    upload_root = Path("data/uploads")

    from collections import defaultdict
    cits_by_item: dict[str, list[ScopeCitation]] = defaultdict(list)
    for c in cits:
        cits_by_item[c.scope_item_id].append(c)

    for case in _TEST_CASES:
        find = case["find"]
        sheet = case["sheet"]
        query = case["query"]

        # Locate the matching scope item
        match = None
        for it in items:
            if find.lower() in (it.description or "").lower():
                match = it
                break
        print("\n" + "=" * 100)
        print(f"TEST CASE: {find!r}  →  sheet {sheet}")
        print("=" * 100)
        if match:
            print(f"\nScopeItem: {match.csi_code}  {match.description[:90]}")
            drw = [c for c in cits_by_item[match.id] if c.evidence_type == "drawing"]
            print(f"\n--- CURRENT DRAWING EVIDENCE (n={len(drw)}) ---")
            for c in drw[:5]:
                ch = chunk_by_id.get(c.chunk_id)
                text = (ch.text if ch else c.excerpt or "")[:240]
                print(f"  [sheet={c.sheet_number} page={c.page_number}] {text!r}")
        else:
            print(f"\n(no scope item matched {find!r} in description)")

        # Vision pass
        img_rel = sheet_to_image.get(sheet)
        if not img_rel:
            print(f"\n--- VISION CALL ---\n  (no rendered image for sheet {sheet})")
            continue
        img_path = upload_root / img_rel
        if not img_path.exists():
            print(f"\n--- VISION CALL ---\n  (image not found at {img_path})")
            continue

        print(f"\n--- VISION CALL on {img_path.name} ---")
        try:
            text, usage = await _vision_query(img_path, query)
            print(text)
            print(f"\n[tokens: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}]")
        except Exception as e:
            print(f"  vision call failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
