"""Tests for the document intake, status, history and report routes."""

from __future__ import annotations

import uuid
from typing import Any

import boto3
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.config import settings
from app.models import Document, Status


def _create_document(client: TestClient, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "filename": "claim.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1024,
    }
    payload.update(overrides)
    return client.post("/api/documents", json=payload)


def _put_uploaded_object(document_id: str) -> None:
    s3_client = boto3.client("s3", region_name=settings.aws_region)
    s3_client.put_object(
        Bucket=settings.s3_bucket, Key=f"{settings.upload_prefix}{document_id}", Body=b"hello"
    )


def test_create_document_returns_upload_slot_and_uploading_row(client: TestClient) -> None:
    response = _create_document(client)

    assert response.status_code == 201
    body = response.json()
    assert "upload_url" in body
    assert body["fields"]["key"] == f"uploads/{body['document_id']}"

    fetched = client.get(f"/api/documents/{body['document_id']}").json()
    assert fetched["status"] == Status.UPLOADING.value


def test_create_document_rejects_disallowed_content_type(client: TestClient) -> None:
    response = _create_document(client, content_type="application/zip")
    assert response.status_code == 415


def test_create_document_rejects_oversized_file(client: TestClient) -> None:
    response = _create_document(client, size_bytes=settings.max_upload_bytes + 1)
    assert response.status_code == 413


def test_presigned_key_is_uploads_prefix_document_id_with_no_filename(
    client: TestClient,
) -> None:
    response = _create_document(client, filename="report with spaces & stuff.pdf")
    body = response.json()

    assert body["fields"]["key"] == f"uploads/{body['document_id']}"


def test_upload_complete_moves_uploading_to_queued(client: TestClient) -> None:
    document_id = _create_document(client).json()["document_id"]
    _put_uploaded_object(document_id)

    response = client.post(f"/api/documents/{document_id}/upload-complete")

    assert response.status_code == 200
    assert response.json()["status"] == Status.QUEUED.value


def test_upload_complete_without_object_is_409_and_leaves_status_unchanged(
    client: TestClient,
) -> None:
    document_id = _create_document(client).json()["document_id"]

    response = client.post(f"/api/documents/{document_id}/upload-complete")

    assert response.status_code == 409
    fetched = client.get(f"/api/documents/{document_id}").json()
    assert fetched["status"] == Status.UPLOADING.value


def test_upload_complete_does_not_move_processing_backwards(
    client: TestClient, db_session: Session
) -> None:
    """The race guard: the worker already claimed the document (status=PROCESSING) before
    upload-complete arrived, because the S3 event beat the browser's call. The conditional
    update must match zero rows and leave the worker's claim alone. This is the most important
    test in the file."""
    document_id = _create_document(client).json()["document_id"]
    _put_uploaded_object(document_id)

    document = db_session.get(Document, uuid.UUID(document_id))
    assert document is not None
    document.status = Status.PROCESSING.value
    db_session.commit()

    response = client.post(f"/api/documents/{document_id}/upload-complete")

    assert response.status_code == 200
    assert response.json()["status"] == Status.PROCESSING.value


def test_list_documents_is_newest_first_and_respects_limit(client: TestClient) -> None:
    ids = [_create_document(client).json()["document_id"] for _ in range(3)]

    response = client.get("/api/documents", params={"limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["id"] == ids[-1]
    assert body["items"][1]["id"] == ids[-2]


def test_report_404_for_unknown_document(client: TestClient) -> None:
    response = client.get(f"/api/documents/{uuid.uuid4()}/report")
    assert response.status_code == 404


def test_report_409_when_not_ready(client: TestClient) -> None:
    document_id = _create_document(client).json()["document_id"]

    response = client.get(f"/api/documents/{document_id}/report")

    assert response.status_code == 409


def test_healthz_survives_dead_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import db as db_module

    dead_engine = create_engine("postgresql+psycopg://docintel:docintel@localhost:1/docintel")
    monkeypatch.setattr(db_module, "engine", dead_engine)

    response = client.get("/healthz")

    assert response.status_code == 200
