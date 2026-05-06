"""P3 — YOLO11 MEP symbol pre-pass (W3 mitigation).

Per the design doc, MEP pages (FP / Plumbing / Mechanical / Electrical)
have dense overlapping symbology that LLM vision models struggle with.
A YOLO11 fine-tuned classifier sidesteps this by feeding
`(symbol_type, count, bbox)` tuples into the discipline agent's
context — the agent sees structured symbol data instead of trying to
read symbols out of pixels.

Inference happens at the discipline_agent step before the Sonnet call.

This module is a STUB until you have a fine-tuned model:
  - When `ultralytics` is installed AND `YOLO_MEP_MODEL_PATH` env var
    points at a real model file, runs real inference
  - When either is missing, returns an empty list (graceful fallback)
    and the discipline agent runs without symbol pre-pass

To enable real inference:
  1. `uv add ultralytics` (~150MB; pulls torch)
  2. Train or download a YOLO11 model fine-tuned on AEC MEP symbols
     (e.g. via Roboflow Universe — search "AEC symbols")
  3. Set YOLO_MEP_MODEL_PATH=/path/to/best.pt in .env
  4. Restart backend

Without ultralytics, the discipline agent still runs and produces
useful output for MEP disciplines from spec text + drawing chunks +
typed schedules. YOLO is an enhancement, not a hard dependency.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)


@dataclass
class DetectedSymbol:
    symbol_type: str
    confidence: float
    bbox: tuple[int, int, int, int]  # (x0, y0, x1, y1) in image pixels
    page_number: int


def is_available() -> bool:
    """True iff both the package and a model file are present."""
    if not getattr(settings, "yolo_mep_model_path", None):
        return False
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        return False
    return Path(settings.yolo_mep_model_path).exists()


_model = None


def _get_model():
    global _model
    if _model is not None:
        return _model
    if not is_available():
        return None
    from ultralytics import YOLO

    _model = YOLO(settings.yolo_mep_model_path)
    log.info("yolo_mep: loaded model from %s", settings.yolo_mep_model_path)
    return _model


def _detect_sync(
    image_path: Path,
    page_number: int,
    *,
    conf_threshold: float = 0.30,
) -> list[DetectedSymbol]:
    """Sync inference; called from `detect()` via to_thread."""
    model = _get_model()
    if model is None:
        return []
    results = model.predict(
        source=str(image_path),
        conf=conf_threshold,
        verbose=False,
    )
    out: list[DetectedSymbol] = []
    for result in results:
        names = result.names
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        for box in boxes:
            cls_id = int(box.cls.item()) if hasattr(box.cls, "item") else int(box.cls)
            conf = float(box.conf.item()) if hasattr(box.conf, "item") else float(box.conf)
            xyxy = box.xyxy[0].tolist() if hasattr(box.xyxy, "tolist") else list(box.xyxy[0])
            out.append(
                DetectedSymbol(
                    symbol_type=names.get(cls_id, f"cls_{cls_id}"),
                    confidence=conf,
                    bbox=tuple(int(v) for v in xyxy),
                    page_number=page_number,
                )
            )
    return out


async def detect(
    image_path: Path,
    page_number: int,
    *,
    conf_threshold: float = 0.30,
) -> list[DetectedSymbol]:
    """Async wrapper. Returns empty list if YOLO isn't configured."""
    if not is_available():
        return []
    return await asyncio.to_thread(
        _detect_sync, image_path, page_number, conf_threshold=conf_threshold
    )


async def detect_for_discipline(
    project_id: str, discipline_key: str
) -> list[dict]:
    """Run YOLO over every drawing page for a given MEP discipline.

    Returns aggregated `[{symbol_type, count, sample_bboxes}]` ready to
    drop into the discipline_agent's context.

    No-op for non-MEP disciplines (architectural / structural / etc.).
    """
    if discipline_key not in ("fire-protection", "plumbing", "mechanical", "electrical"):
        return []
    if not is_available():
        return []

    from sqlalchemy import select

    from ..database import SessionLocal
    from ..models import Document, DocumentPage, SheetIndex
    from .storage import storage

    async with SessionLocal() as db:
        # Find the sheets owned by this discipline (via SheetIndex)
        sheet_ids = (
            await db.execute(
                select(SheetIndex.sheet_id)
                .where(SheetIndex.project_id == project_id)
                .where(SheetIndex.discipline == discipline_key)
            )
        ).scalars().all()
        if not sheet_ids:
            return []
        # Find the rendered pages (via PageExtraction.sheet_number match)
        from ..models import PageExtraction

        pe_rows = (
            await db.execute(
                select(PageExtraction, DocumentPage)
                .join(DocumentPage, PageExtraction.page_id == DocumentPage.id)
                .join(Document, PageExtraction.document_id == Document.id)
                .where(Document.project_id == project_id)
                .where(PageExtraction.sheet_number.in_(sheet_ids))
            )
        ).all()
        targets = [
            (storage.absolute_path(dp.image_path), pe.page_number)
            for pe, dp in pe_rows
            if dp.image_path
        ]

    if not targets:
        return []

    # Run inference on each target page
    detections: list[DetectedSymbol] = []
    for path, page_n in targets:
        if not path.exists():
            continue
        det = await detect(path, page_n)
        detections.extend(det)

    # Aggregate by symbol_type
    counts: dict[str, dict] = {}
    for d in detections:
        bucket = counts.setdefault(
            d.symbol_type,
            {"symbol_type": d.symbol_type, "count": 0, "sample_bboxes": []},
        )
        bucket["count"] += 1
        if len(bucket["sample_bboxes"]) < 3:
            bucket["sample_bboxes"].append(
                {"page": d.page_number, "bbox": list(d.bbox)}
            )
    return list(counts.values())
