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
    doc_type: str | None
    created_at: datetime
