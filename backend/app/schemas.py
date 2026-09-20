"""Request and response models.

`DocumentOut` mirrors the `Document` column names directly, because it is read straight off
the ORM object (`from_attributes=True`). The upload-slot request and response are shaped by
the endpoint's own contract instead, not by the table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DocumentCreateRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


class DocumentCreateResponse(BaseModel):
    document_id: uuid.UUID
    upload_url: str
    fields: dict[str, str]


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    outcome: str | None
    doc_type: str | None
    current_step: str | None
    report_summary: dict[str, Any] | None
    error_message: str | None
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    uploaded_at: datetime | None
    completed_at: datetime | None


class DocumentListResponse(BaseModel):
    items: list[DocumentOut]
    limit: int
    offset: int
