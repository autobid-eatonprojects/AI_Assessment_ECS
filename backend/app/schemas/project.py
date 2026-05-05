from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LifecycleState = Literal["setup", "open-for-bids", "complete"]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class LifecycleTransition(BaseModel):
    """Request body for POST /projects/{id}/lifecycle.

    Allowed transitions:
        setup           → open-for-bids   (lock scope, accept bids)
        open-for-bids   → complete         (analysis done)
        open-for-bids   → setup            (re-open scope; rare)
    """

    new_state: LifecycleState


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    lifecycle_state: LifecycleState
    created_at: datetime
    updated_at: datetime
    document_count: int = 0
    project_document_count: int = 0
    bid_submission_count: int = 0
