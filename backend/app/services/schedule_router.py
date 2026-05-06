"""P2 — Schedule router.

For every drawing-set page, asks Haiku 4.5 (cheap, fast) to look at the
page's THUMBNAIL and decide:
  - Does this page contain one or more schedules / tabular data?
  - If yes, what type (door / window / finish / fixture / room / panel /
    equipment / other)?
  - What's the bbox (normalized [0,1]) of the schedule region?

Pages flagged as schedules feed into the per-type schedule_extractor
(Sonnet vision on 600 DPI tile crops with type-specific Pydantic
schemas) for high-fidelity grid recovery (W6 mitigation).

Pages NOT flagged stay covered by the existing generic vision_extractor
plus the P3 discipline agents — nothing is lost by routing.

Cost:
  ~1 Haiku call per drawing page × ~$0.0005 = pennies per project.
  Tool schema is cached (cache_control on the tool definition).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Document, DocumentPage
from .llm_log import record_call, usage_from_anthropic
from .storage import storage

log = logging.getLogger(__name__)


# 7 schedule types per design doc P2 + "other" + "none"
SCHEDULE_TYPES = [
    "door",
    "window",
    "finish",
    "room",
    "fixture",
    "panel",
    "equipment",
    "other",
]


_ROUTE_TOOL = {
    "name": "classify_schedules",
    "description": (
        "Decide whether this drawing page contains any schedules / tabular "
        "data (the formatted tables an estimator counts off — door, "
        "window, finish, equipment, panel schedules etc.). Return one entry "
        "per distinct schedule visible on the page. Empty list = no "
        "schedules. Use the bbox to mark each schedule's region."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "schedules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": SCHEDULE_TYPES,
                            "description": (
                                "Schedule type (door, window, finish, room, "
                                "fixture, panel, equipment) — guess by the "
                                "header / column structure. 'other' for "
                                "schedules that don't match the standard "
                                "AEC categories (notes/calc tables don't "
                                "count as schedules)."
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "The schedule's printed name as visible on "
                                "the page (e.g. 'DOOR SCHEDULE', "
                                "'EQUIPMENT SCHEDULE - HVAC')"
                            ),
                        },
                        "bbox": {
                            "type": "object",
                            "description": (
                                "Normalised [0,1] bbox of the schedule "
                                "region; (0,0) top-left, (1,1) bottom-right"
                            ),
                            "properties": {
                                "x0": {"type": "number", "minimum": 0, "maximum": 1},
                                "y0": {"type": "number", "minimum": 0, "maximum": 1},
                                "x1": {"type": "number", "minimum": 0, "maximum": 1},
                                "y1": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["x0", "y0", "x1", "y1"],
                        },
                    },
                    "required": ["type", "name", "bbox"],
                },
            }
        },
        "required": ["schedules"],
    },
    "cache_control": {"type": "ephemeral"},
}


_SYSTEM_PROMPT = """\
You are scanning construction drawing pages to identify schedule tables — \
the formatted, gridded data an estimator reads to count fixtures, doors, \
equipment, etc.

What COUNTS as a schedule:
  - Tables with column headers + multiple rows of items
  - Door schedules, window schedules, finish schedules, room schedules,
    fixture schedules, panel schedules, equipment schedules
  - Footing / foundation / column schedules (categorize as 'other')

What does NOT count:
  - Plain notes blocks ("GENERAL NOTES" with paragraph text)
  - Code reference tables, design loads tables (these are reference
    data, not biddable items)
  - Legend / symbology blocks
  - Cross-reference lists ("see X for Y")

For each detected schedule, give the bbox in normalised page coordinates \
[0,1]. Be tight on the bbox — exclude title bars and surrounding white \
space — because the bbox is used to crop the schedule for high-DPI \
re-extraction.

If there are no schedules, return an empty list. Don't invent.
"""


@dataclass
class _RoutedSchedule:
    schedule_type: str
    name: str
    bbox: dict
    page_id: str
    page_number: int
    document_id: str


@dataclass
class RouteResult:
    project_id: str
    pages_classified: int
    schedules_found: int
    cost_usd: float
    schedules: list[_RoutedSchedule]


_client = None
_concurrency_sem: asyncio.Semaphore | None = None
_ROUTE_CONCURRENCY = 8


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.anthropic_api_key:
        return None
    from anthropic import AsyncAnthropic

    _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _get_sem() -> asyncio.Semaphore:
    global _concurrency_sem
    if _concurrency_sem is None:
        _concurrency_sem = asyncio.Semaphore(_ROUTE_CONCURRENCY)
    return _concurrency_sem


async def _classify_one_page(
    client,
    *,
    project_id: str,
    document_id: str,
    page: DocumentPage,
) -> tuple[list[_RoutedSchedule], float]:
    """One Haiku call against this page's full-res image.

    Despite the design doc saying "thumbnail", we use the 150-DPI full
    render here — the 320-px thumbnail is too small for Haiku to make
    out schedule grid structure (rows would be 3-5 px tall on an
    arch-D sheet). Cost difference is negligible at Haiku rates.
    """
    if not page.image_path:
        return [], 0.0
    # DocumentPage.image_path is project-relative
    image_path = storage.absolute_path(page.image_path)
    if not image_path.exists():
        return [], 0.0

    # Auto-downsamples to JPEG if the page exceeds Anthropic's 5MB cap.
    from .renderer import read_image_for_vision

    raw, media_type = read_image_for_vision(image_path)
    b64 = base64.standard_b64encode(raw).decode("ascii")

    sem = _get_sem()
    async with sem:
        t0 = time.perf_counter()
        try:
            msg = await client.messages.create(
                model=settings.classifier_model,  # Haiku 4.5
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[_ROUTE_TOOL],
                tool_choice={"type": "tool", "name": "classify_schedules"},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Identify any schedules on this drawing page."
                                ),
                            },
                        ],
                    }
                ],
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "schedule_router: page %d failed: %s", page.page_number, e
            )
            return [], 0.0
        latency_ms = int((time.perf_counter() - t0) * 1000)

    payload: dict = {}
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "classify_schedules"
        ):
            payload = block.input
            break

    cost: float = 0.0
    async with SessionLocal() as db:
        c, _ = await record_call(
            db,
            purpose="schedule-route",
            model=settings.classifier_model,
            usage=usage_from_anthropic(msg),
            latency_ms=latency_ms,
            project_id=project_id,
            document_id=document_id,
        )
        cost = c or 0.0
        await db.commit()

    routed: list[_RoutedSchedule] = []
    for entry in payload.get("schedules") or []:
        # Defensive: tool schema enforces dict shape but the model
        # occasionally slips a string in instead — skip those rather
        # than raising and losing the whole page's classifications.
        if not isinstance(entry, dict):
            log.warning(
                "schedule_router: page %d returned non-dict entry %r — skipping",
                page.page_number, entry,
            )
            continue
        routed.append(
            _RoutedSchedule(
                schedule_type=entry.get("type") or "other",
                name=(entry.get("name") or "").strip(),
                bbox=entry.get("bbox") or {},
                page_id=page.id,
                page_number=page.page_number,
                document_id=document_id,
            )
        )
    return routed, cost


async def route_for_document(document_id: str) -> RouteResult:
    """Classify every page of a drawing-set document for schedule presence."""
    client = _get_client()
    if client is None:
        log.warning("schedule_router: no ANTHROPIC_API_KEY; skipping")
        return RouteResult(
            project_id="", pages_classified=0, schedules_found=0,
            cost_usd=0.0, schedules=[],
        )

    async with SessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            raise ValueError(f"document {document_id} not found")
        if doc.doc_type != "drawing-set":
            return RouteResult(
                project_id=doc.project_id, pages_classified=0,
                schedules_found=0, cost_usd=0.0, schedules=[],
            )
        pages = (
            await db.execute(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_number)
            )
        ).scalars().all()
        project_id = doc.project_id

    if not pages:
        return RouteResult(
            project_id=project_id, pages_classified=0,
            schedules_found=0, cost_usd=0.0, schedules=[],
        )

    log.info(
        "schedule_router: classifying %d pages for %s", len(pages), document_id
    )
    tasks = [
        _classify_one_page(
            client, project_id=project_id, document_id=document_id, page=p
        )
        for p in pages
    ]
    results = await asyncio.gather(*tasks)

    all_routed: list[_RoutedSchedule] = []
    total_cost = 0.0
    for routed, cost in results:
        all_routed.extend(routed)
        total_cost += cost

    log.info(
        "schedule_router: %s — %d pages classified, %d schedules found, $%.4f",
        document_id, len(pages), len(all_routed), total_cost,
    )
    return RouteResult(
        project_id=project_id,
        pages_classified=len(pages),
        schedules_found=len(all_routed),
        cost_usd=total_cost,
        schedules=all_routed,
    )


async def route_for_project(project_id: str) -> dict:
    """Run the schedule router against every drawing-set in a project."""
    async with SessionLocal() as db:
        docs = (
            await db.execute(
                select(Document)
                .where(Document.project_id == project_id)
                .where(Document.doc_type == "drawing-set")
            )
        ).scalars().all()

    total_pages = 0
    total_schedules = 0
    total_cost = 0.0
    per_doc: list[RouteResult] = []
    for doc in docs:
        try:
            r = await route_for_document(doc.id)
            per_doc.append(r)
            total_pages += r.pages_classified
            total_schedules += r.schedules_found
            total_cost += r.cost_usd
        except Exception as e:  # noqa: BLE001
            log.exception(
                "schedule_router: failed on %s: %s", doc.filename, e
            )

    return {
        "project_id": project_id,
        "documents": len(docs),
        "pages_classified": total_pages,
        "schedules_found": total_schedules,
        "cost_usd": total_cost,
        "results": per_doc,
    }
