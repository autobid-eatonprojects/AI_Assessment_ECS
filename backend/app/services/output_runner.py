"""Stage 10 — orchestrate per-run output generation.

Invokes ``sow_writer.write_sows_for_run`` and ``gap_report.write_gap_report``,
persists output paths via AuditLog rows tagged ``entity_type='output'``.

The frontend Outputs page reads these AuditLog rows to render the file
list and download links. Files are written under
``backend/data/outputs/{project_id}/{run_id}/...``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import AuditLog, ScopeExtractionRun
from .audit import record_audit
from .gap_report import write_gap_report
from .sow_writer import default_api_base_url, write_sows_for_run

log = logging.getLogger(__name__)


def _output_dir_for(project_id: str, run_id: str) -> Path:
    return (settings.storage_root.parent / "outputs" / project_id / run_id).resolve()


async def generate_outputs(
    project_id: str,
    *,
    run_id: str | None = None,
    actor: str = "system:output_runner",
    api_base_url: str | None = None,
) -> dict:
    """Generate SOWs (per package) + a gap report PDF for a run.

    If ``run_id`` is None, picks the latest run for the project.
    Returns a summary dict the API surfaces back to the UI.
    """
    if api_base_url is None:
        api_base_url = default_api_base_url()

    async with SessionLocal() as db:
        if run_id is None:
            row = (
                await db.execute(
                    select(ScopeExtractionRun.id)
                    .where(ScopeExtractionRun.project_id == project_id)
                    .order_by(ScopeExtractionRun.started_at.desc())
                    .limit(1)
                )
            ).first()
            run_id = row[0] if row else None
        if run_id is None:
            raise ValueError("output_runner: no scope run found for project")

    output_dir = _output_dir_for(project_id, run_id)

    log.info("output_runner: writing outputs for run %s to %s", run_id, output_dir)
    sows = await write_sows_for_run(
        run_id, output_dir=output_dir, api_base_url=api_base_url
    )
    report = await write_gap_report(run_id, output_dir=output_dir)

    # Persist outputs as AuditLog rows so the frontend can list them.
    started_at = datetime.now(timezone.utc).isoformat()
    async with SessionLocal() as db:
        for s in sows:
            await record_audit(
                db,
                project_id=project_id,
                run_id=run_id,
                action="export",
                entity_type="output",
                entity_id=s.package_id,
                actor=actor,
                payload={
                    "kind": "sow_docx",
                    "package_label": s.package_label,
                    "filename": s.file_path.name,
                    "size_bytes": s.file_size,
                    "item_count": s.item_count,
                    "generated_at": started_at,
                },
            )
        if report is not None:
            await record_audit(
                db,
                project_id=project_id,
                run_id=run_id,
                action="export",
                entity_type="output",
                actor=actor,
                payload={
                    "kind": "gap_report_pdf",
                    "filename": report.file_path.name,
                    "size_bytes": report.file_size,
                    "page_count": report.page_count,
                    "generated_at": started_at,
                },
            )
        await db.commit()

    return {
        "run_id": run_id,
        "output_dir": str(output_dir),
        "sows": [
            {
                "package_id": s.package_id,
                "package_label": s.package_label,
                "filename": s.file_path.name,
                "size_bytes": s.file_size,
                "item_count": s.item_count,
            }
            for s in sows
        ],
        "gap_report": (
            {
                "filename": report.file_path.name,
                "size_bytes": report.file_size,
                "page_count": report.page_count,
            }
            if report is not None
            else None
        ),
    }


async def list_outputs(project_id: str, *, run_id: str | None = None) -> list[dict]:
    """Return generated output metadata from AuditLog (newest first)."""
    async with SessionLocal() as db:
        q = (
            select(AuditLog)
            .where(AuditLog.project_id == project_id)
            .where(AuditLog.entity_type == "output")
            .order_by(AuditLog.created_at.desc())
        )
        if run_id is not None:
            q = q.where(AuditLog.run_id == run_id)
        rows = (await db.execute(q)).scalars().all()

    out: list[dict] = []
    for r in rows:
        payload = r.payload or {}
        out.append(
            {
                "audit_id": r.id,
                "run_id": r.run_id,
                "kind": payload.get("kind"),
                "filename": payload.get("filename"),
                "size_bytes": payload.get("size_bytes"),
                "item_count": payload.get("item_count"),
                "page_count": payload.get("page_count"),
                "package_label": payload.get("package_label"),
                "created_at": r.created_at.isoformat(),
                "actor": r.actor,
            }
        )
    return out


def output_path(project_id: str, run_id: str, filename: str) -> Path:
    """Resolve the on-disk path for a generated output, with traversal guard."""
    safe = Path(filename).name  # strip any directory components
    return _output_dir_for(project_id, run_id) / safe
