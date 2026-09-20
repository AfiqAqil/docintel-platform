"""The document routes: request an upload slot, confirm it, list, fetch, and read the report.

See ARCHITECTURE section 2 for the sequence and section 3 for the status model. The two
guards on `upload-complete` are described there and are not re-derived here.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import s3
from app.config import settings
from app.db import get_db
from app.logging import set_document_id
from app.models import TERMINAL_STATUSES, Document, Status
from app.schemas import (
    DocumentCreateRequest,
    DocumentCreateResponse,
    DocumentListResponse,
    DocumentOut,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_TERMINAL_VALUES = {status.value for status in TERMINAL_STATUSES}


@router.post("", response_model=DocumentCreateResponse, status_code=201)
def create_document(
    body: DocumentCreateRequest, db: Session = Depends(get_db)  # noqa: B008
) -> DocumentCreateResponse:
    if body.content_type not in settings.allowed_content_types:
        raise HTTPException(status_code=415, detail="unsupported content type")
    if body.size_bytes > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="file too large")

    document = Document(
        id=uuid.uuid4(),
        filename=body.filename,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        status=Status.UPLOADING.value,
    )
    db.add(document)
    db.commit()

    set_document_id(str(document.id))
    logger.info("document created, upload slot requested")

    key = f"{settings.upload_prefix}{document.id}"
    presigned = s3.presign_upload(key, body.content_type, body.filename)
    return DocumentCreateResponse(
        document_id=document.id, upload_url=presigned["url"], fields=presigned["fields"]
    )


@router.get("", response_model=DocumentListResponse)
def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),  # noqa: B008
) -> DocumentListResponse:
    rows = (
        db.execute(
            select(Document).order_by(Document.created_at.desc()).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )
    items = [DocumentOut.model_validate(row) for row in rows]
    return DocumentListResponse(items=items, limit=limit, offset=offset)


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(document_id: uuid.UUID, db: Session = Depends(get_db)) -> Document:  # noqa: B008
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return document


@router.post("/{document_id}/upload-complete", response_model=DocumentOut)
def upload_complete(
    document_id: uuid.UUID, db: Session = Depends(get_db)  # noqa: B008
) -> Document:
    set_document_id(str(document_id))
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")

    key = f"{settings.upload_prefix}{document_id}"
    if not s3.object_exists(key):
        # A client claiming an upload that never happened. Nothing to update.
        raise HTTPException(status_code=409, detail="object not found in S3")

    # Conditional on UPLOADING: if the worker already claimed the document, the S3 event beat
    # this call, and this must match zero rows and leave the document alone. That is a normal
    # race, not a failure, so the row count is not checked.
    db.execute(
        update(Document)
        .where(Document.id == document_id, Document.status == Status.UPLOADING.value)
        .values(status=Status.QUEUED.value, uploaded_at=func.now())
    )
    db.commit()
    db.refresh(document)
    logger.info("upload-complete processed, status=%s", document.status)
    return document


@router.get("/{document_id}/report")
def get_report(document_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:  # noqa: B008
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    if document.status not in _TERMINAL_VALUES:
        raise HTTPException(status_code=409, detail="report not ready")

    report = s3.get_json(f"{settings.report_prefix}{document_id}.json")
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return report
