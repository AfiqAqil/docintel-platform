"""Configuration, read once from the environment.

The database password is deliberately not an environment variable on AWS. RDS rotates the
managed master password roughly every 7 days, and ECS resolves a task definition's secrets
only at task start, so a long running task would hold a stale password and lose the database
mid-life. The password is fetched from Secrets Manager with boto3 when a connection pool is
opened, cached, and refetched once on an authentication error.
"""

from __future__ import annotations

import functools
import json
import os
from urllib.parse import quote


class Settings:
    # Database. DATABASE_URL is the local and compose path; on AWS the host and the secret
    # arn are set instead and the password is fetched at runtime.
    database_url_override: str | None = os.environ.get("DATABASE_URL")
    db_host: str = os.environ.get("DB_HOST", "localhost")
    db_port: int = int(os.environ.get("DB_PORT", 5432))
    db_name: str = os.environ.get("DB_NAME", "docintel")
    db_user: str = os.environ.get("DB_USER", "docintel")
    db_secret_arn: str | None = os.environ.get("DB_SECRET_ARN")

    aws_region: str = os.environ.get("AWS_REGION", "ap-southeast-1")
    # Set locally to point boto3 at LocalStack. Unset on AWS, where the real endpoints apply.
    aws_endpoint_url: str | None = os.environ.get("AWS_ENDPOINT_URL")

    s3_bucket: str = os.environ.get("S3_BUCKET", "docintel-local")
    upload_prefix: str = os.environ.get("UPLOAD_PREFIX", "uploads/")
    report_prefix: str = os.environ.get("REPORT_PREFIX", "reports/")

    # Presigned POST policy limits. Both are enforced by S3 itself, not by us, so a client
    # cannot talk its way past them.
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_BYTES", 20 * 1024 * 1024))
    presign_expiry_seconds: int = int(os.environ.get("PRESIGN_EXPIRY_SECONDS", 300))

    allowed_content_types: tuple[str, ...] = tuple(
        os.environ.get(
            "ALLOWED_CONTENT_TYPES",
            "application/pdf,"
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
            "image/png,image/jpeg,image/tiff,image/webp",
        ).split(",")
    )

    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        password = quote(_db_password(self.db_secret_arn, self.aws_region), safe="")
        return (
            f"postgresql+psycopg://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@functools.lru_cache(maxsize=1)
def _db_password(secret_arn: str | None, region: str) -> str:
    if not secret_arn:
        return os.environ.get("DB_PASSWORD", "docintel")

    import boto3

    client = boto3.client("secretsmanager", region_name=region)
    raw = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    # RDS managed secrets are JSON with a password key. Accept a bare string too, so the
    # same code path works if the secret is ever set by hand.
    try:
        return str(json.loads(raw)["password"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return raw


def forget_db_password() -> None:
    """Drop the cached password, so the next connection refetches it.

    Called once on an authentication error. RDS rotates the managed master password, and a
    task that has been running since before a rotation holds the old one.
    """
    _db_password.cache_clear()


settings = Settings()
