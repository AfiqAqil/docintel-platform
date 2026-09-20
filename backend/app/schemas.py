"""Request and response models.

`DocumentOut` mirrors the `Document` column names directly, because it is read straight off
the ORM object (`from_attributes=True`). The upload-slot request and response are shaped by
the endpoint's own contract instead, not by the table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentCreateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    content_type: str
    # Bounded below as well as above. Without a lower bound a negative size passes both this
    # schema and the endpoint's upper bound check, creating a row whose metadata is
    # impossible while S3 goes on to accept a perfectly real object. Zero is excluded too: an
    # empty file is not a document, and accepting one only creates a row that can do nothing
    # but fail later.
    size_bytes: int = Field(gt=0)


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
