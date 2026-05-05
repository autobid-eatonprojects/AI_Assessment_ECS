import mimetypes

from fastapi import APIRouter, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from ..config import settings
from ..models import Document, DocumentPage, Project
from ..schemas import DocumentOut, DocumentPageOut
from ..services import processor
from ..services.storage import storage
from .deps import DB, CurrentUser

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
    project_id: str, db: DB, _: CurrentUser, files: list[UploadFile]
) -> list[DocumentOut]:
    await _ensure_project(db, project_id)
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no files uploaded")

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

        doc = Document(
            project_id=project_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            storage_path="",
            sha256="",
            processing_status="pending",
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
