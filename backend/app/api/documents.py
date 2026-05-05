import logging
import mimetypes
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from ..config import settings
from ..models import Document, DocumentPage, Project
from ..schemas import DocumentOut, DocumentPageOut, DocumentPageTextOut
from ..services import processor
from ..services.image_mime import detect_image_mime
from ..services.storage import storage
from .deps import DB, CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/documents", tags=["documents"])


async def _ensure_project(db, project_id: str) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


async def _ensure_document(db, project_id: str, document_id: str) -> Document:
    result = await db.execute(
        select(Document).where(
            Document.id == document_id, Document.project_id == project_id
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    return doc


@router.get("", response_model=list[DocumentOut])
async def list_documents(project_id: str, db: DB, _: CurrentUser) -> list[DocumentOut]:
    await _ensure_project(db, project_id)
    result = await db.execute(
        select(Document)
        .where(Document.project_id == project_id)
        .order_by(Document.created_at.desc())
    )
    return [DocumentOut.model_validate(d) for d in result.scalars().all()]


@router.post("", response_model=list[DocumentOut], status_code=status.HTTP_201_CREATED)
async def upload_documents(
    project_id: str,
    db: DB,
    _: CurrentUser,
    files: list[UploadFile],
    source: Annotated[str, Form()] = "project_document",
    vendor_name: Annotated[str | None, Form()] = None,
) -> list[DocumentOut]:
    """Upload one or more documents.

    `source` controls which side of the workflow the document belongs to:
        - "project_document"  (default) — drawings, specs, trade list, etc.
                              Allowed only while project is in 'setup'.
        - "bid_submission"    — vendor bids and qualification attachments.
                              Allowed only when project is 'open-for-bids';
                              vendor_name required.
    """
    if source not in ("project_document", "bid_submission"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid source '{source}'",
        )

    project = await _ensure_project(db, project_id)
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no files uploaded")

    # Lifecycle gating: which uploads are allowed in which state.
    if source == "project_document" and project.lifecycle_state not in ("setup",):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"project is in '{project.lifecycle_state}' — re-open scope "
                "(transition to 'setup') to add more project documents"
            ),
        )
    if source == "bid_submission":
        if project.lifecycle_state != "open-for-bids":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"project is in '{project.lifecycle_state}' — bid submissions "
                    "can only be uploaded once scope is locked (open-for-bids)"
                ),
            )
        if not (vendor_name and vendor_name.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vendor_name is required when uploading a bid submission",
            )
    elif vendor_name:
        # vendor_name on a project_document is meaningless — drop it
        vendor_name = None

    out: list[Document] = []
    for f in files:
        data = await f.read()
        if len(data) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"empty file: {f.filename}",
            )
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{f.filename} exceeds {settings.max_upload_mb} MB limit",
            )

        filename = f.filename or "unnamed"
        content_type = (
            f.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )

        # Trust the bytes, not the extension. If a JPEG was renamed `.png`
        # the browser hands us `image/png` and downstream API calls 400 on
        # the magic-number mismatch. Sniff the first 16 bytes once and
        # override content_type when the truth disagrees.
        sniffed = detect_image_mime(data[:16])
        if sniffed and sniffed != content_type:
            log.info(
                "upload: %s declared %s, sniffed %s — using sniffed",
                filename,
                content_type,
                sniffed,
            )
            content_type = sniffed

        doc = Document(
            project_id=project_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            storage_path="",
            sha256="",
            processing_status="pending",
            source=source,
            vendor_name=vendor_name.strip() if vendor_name else None,
        )
        db.add(doc)
        await db.flush()

        rel_path, sha = await storage.save(project_id, doc.id, filename, data)
        doc.storage_path = rel_path
        doc.sha256 = sha
        out.append(doc)

    await db.commit()
    for d in out:
        await db.refresh(d)

    # Kick off background classification + rendering
    for d in out:
        processor.schedule(d.id)

    return [DocumentOut.model_validate(d) for d in out]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    project_id: str, document_id: str, db: DB, _: CurrentUser
) -> DocumentOut:
    doc = await _ensure_document(db, project_id, document_id)
    return DocumentOut.model_validate(doc)


@router.get("/{document_id}/download")
async def download_document(project_id: str, document_id: str, db: DB, _: CurrentUser):
    doc = await _ensure_document(db, project_id, document_id)
    return FileResponse(
        path=storage.absolute_path(doc.storage_path),
        filename=doc.filename,
        media_type=doc.content_type,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(project_id: str, document_id: str, db: DB, _: CurrentUser) -> None:
    doc = await _ensure_document(db, project_id, document_id)

    # Delete page artifacts
    page_result = await db.execute(
        select(DocumentPage).where(DocumentPage.document_id == document_id)
    )
    for page in page_result.scalars().all():
        try:
            storage.delete(page.image_path)
            storage.delete(page.thumbnail_path)
        except Exception:  # noqa: BLE001
            pass  # don't block deletion on missing files

    storage.delete(doc.storage_path)
    await db.delete(doc)
    await db.commit()


@router.post("/{document_id}/reclassify", response_model=DocumentOut)
async def reclassify_document(
    project_id: str, document_id: str, db: DB, _: CurrentUser
) -> DocumentOut:
    doc = await _ensure_document(db, project_id, document_id)
    doc.processing_status = "pending"
    doc.processing_error = None
    await db.commit()
    await db.refresh(doc)
    processor.schedule(doc.id)
    return DocumentOut.model_validate(doc)


@router.get("/{document_id}/pages", response_model=list[DocumentPageOut])
async def list_pages(
    project_id: str, document_id: str, db: DB, _: CurrentUser
) -> list[DocumentPageOut]:
    await _ensure_document(db, project_id, document_id)
    result = await db.execute(
        select(DocumentPage)
        .where(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
    )
    return [DocumentPageOut.model_validate(p) for p in result.scalars().all()]


async def _get_page(db, project_id: str, document_id: str, page_number: int) -> DocumentPage:
    await _ensure_document(db, project_id, document_id)
    result = await db.execute(
        select(DocumentPage).where(
            DocumentPage.document_id == document_id,
            DocumentPage.page_number == page_number,
        )
    )
    page = result.scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="page not found")
    return page


@router.get("/{document_id}/pages/{page_number}/image")
async def get_page_image(
    project_id: str, document_id: str, page_number: int, db: DB, _: CurrentUser
):
    page = await _get_page(db, project_id, document_id, page_number)
    return FileResponse(
        path=storage.absolute_path(page.image_path),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/{document_id}/pages/{page_number}/thumbnail")
async def get_page_thumbnail(
    project_id: str, document_id: str, page_number: int, db: DB, _: CurrentUser
):
    page = await _get_page(db, project_id, document_id, page_number)
    return FileResponse(
        path=storage.absolute_path(page.thumbnail_path),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get(
    "/{document_id}/pages/{page_number}/text", response_model=DocumentPageTextOut
)
async def get_page_text(
    project_id: str, document_id: str, page_number: int, db: DB, _: CurrentUser
) -> DocumentPageTextOut:
    """Return per-page text content (PyMuPDF or Gemini OCR — same shape).

    Used by the page detail view to QA OCR'd content for written-spec docs
    and to show text alongside the image for any digital-text PDF.
    """
    page = await _get_page(db, project_id, document_id, page_number)
    text = page.text_content or ""
    return DocumentPageTextOut(
        page_number=page.page_number,
        width=page.width,
        height=page.height,
        text=text or None,
        text_source=page.text_source,
        char_count=len(text),
    )
