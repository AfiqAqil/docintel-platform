"""Presigned POST generation and HeadObject.

One client per call rather than a module-level singleton, because `moto`'s `mock_aws` patches
botocore per test and a client built before the patch is active would bypass it.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings


def _client(for_browser: bool = False) -> Any:
    """An S3 client. `for_browser` builds one whose signed URLs the browser can reach.

    The presigned POST is signed for a specific host, so the host has to be the one the
    browser will actually connect to. Locally that differs from the one this service uses,
    and signing for the wrong one produces a URL that either fails to resolve or fails its
    signature check. On AWS the two are the same and this collapses to a single client.
    """
    endpoint = settings.aws_endpoint_url
    if for_browser and settings.s3_public_endpoint_url:
        endpoint = settings.s3_public_endpoint_url
    # Signature Version 4, stated rather than left to the default. boto3 signs a presigned
    # POST with the legacy scheme unless told otherwise, and SigV4 is the only one every
    # region and every bucket encryption setting accepts.
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        endpoint_url=endpoint,
        config=Config(signature_version="s3v4"),
    )


def _safe_filename(filename: str) -> str:
    # Content-Disposition is an HTTP header, and the filename is user-supplied. Strip quotes
    # and control characters rather than trying to fully implement RFC 6266 encoding here.
    return "".join(ch for ch in filename if ch not in '"\r\n')


def presign_upload(key: str, content_type: str, filename: str) -> dict[str, Any]:
    """Presigned POST for one upload key.

    The policy enforces exactly what ARCHITECTURE section 2 asks for: the exact key (no
    filename in it), the exact content type, and an upper bound on size. S3 itself enforces
    these at upload time, so a client cannot talk its way past them. `Content-Disposition` is
    set so a later download keeps the original filename, which otherwise lives only in the
    `filename` column.
    """
    disposition = f'attachment; filename="{_safe_filename(filename)}"'
    client = _client(for_browser=True)
    return client.generate_presigned_post(
        Bucket=settings.s3_bucket,
        Key=key,
        Fields={
            "Content-Type": content_type,
            "Content-Disposition": disposition,
        },
        Conditions=[
            {"Content-Type": content_type},
            {"Content-Disposition": disposition},
            ["content-length-range", 0, settings.max_upload_bytes],
        ],
        ExpiresIn=settings.presign_expiry_seconds,
    )


def object_exists(key: str) -> bool:
    """True if the key is really in the bucket. Backs the upload-complete guard: a client
    cannot mark a document uploaded without actually uploading it."""
    client = _client()
    try:
        client.head_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        raise
    return True


def get_json(key: str) -> dict[str, Any] | None:
    """The parsed object at `key`, or None if it does not exist."""
    client = _client()
    try:
        response = client.get_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return None
        raise
    body: dict[str, Any] = json.loads(response["Body"].read())
    return body
