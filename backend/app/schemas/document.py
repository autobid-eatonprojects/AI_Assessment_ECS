from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str

    # Two-stage workflow
    source: str  # "project_document" | "bid_submission"
    vendor_name: str | None

    doc_type: str | None
    classification_confidence: float | None
    classification_reasoning: str | None

    page_count: int | None
    processing_status: str
    processing_error: str | None
    processed_at: datetime | None

    created_at: datetime


class DocumentPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    page_number: int
    width: int
    height: int
    created_at: datetime


class DocumentPageTextOut(BaseModel):
    """Per-page text content (PyMuPDF or OCR — same shape)."""

    model_config = ConfigDict(from_attributes=True)

    page_number: int
    width: int
    height: int
    text: str | None
    text_source: str | None  # "pymupdf" | "ocr-gemini" | None
    char_count: int
