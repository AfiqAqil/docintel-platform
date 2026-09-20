"""Shared fixtures.

The schema comes from the real Alembic migration, run once per session against the real
PostgreSQL at localhost:55432, never from `Base.metadata.create_all`: that migration is the
same one CI cannot run and the worker's own tests run against, so a hand-copied schema here
would be exactly the second copy of the DDL ARCHITECTURE section 3 says must not exist. S3 is
mocked with `moto`, so no AWS call is made.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://docintel:docintel@localhost:55432/docintel"
)
os.environ.setdefault("S3_BUCKET", "docintel-test")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_REGION", "ap-southeast-1")

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine, run_migrations  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema() -> None:
    run_migrations()


@pytest.fixture(autouse=True)
def _clean_database() -> Iterator[None]:
    yield
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE documents"))


@pytest.fixture
def db_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def s3_bucket() -> Iterator[None]:
    with mock_aws():
        client = boto3.client("s3", region_name=settings.aws_region)
        client.create_bucket(
            Bucket=settings.s3_bucket,
            CreateBucketConfiguration={"LocationConstraint": settings.aws_region},
        )
        yield


@pytest.fixture
def client(s3_bucket: None) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
