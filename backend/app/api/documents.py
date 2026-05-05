import mimetypes

from fastapi import APIRouter, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from ..config import settings
from ..models import Document, Project
from ..schemas import DocumentOut
from ..services.storage import storage
from .deps import DB, CurrentUser

router = APIRouter(prefix="/projects/{project_id}/documents", tags=["documents"])


async def _ensure_project(db, project_id: str) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


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
            storage_path="",  # set below
            sha256="",  # set below
        )
        db.add(doc)
        await db.flush()  # generate doc.id

        rel_path, sha = await storage.save(project_id, doc.id, filename, data)
        doc.storage_path = rel_path
        doc.sha256 = sha
        out.append(doc)

    await db.commit()
    for d in out:
        await db.refresh(d)
    return [DocumentOut.model_validate(d) for d in out]


@router.get("/{document_id}/download")
async def download_document(project_id: str, document_id: str, db: DB, _: CurrentUser):
    result = await db.execute(
        select(Document).where(
            Document.id == document_id, Document.project_id == project_id
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    return FileResponse(
        path=storage.absolute_path(doc.storage_path),
        filename=doc.filename,
        media_type=doc.content_type,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(project_id: str, document_id: str, db: DB, _: CurrentUser) -> None:
    result = await db.execute(
        select(Document).where(
            Document.id == document_id, Document.project_id == project_id
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    storage.delete(doc.storage_path)
    await db.delete(doc)
    await db.commit()
