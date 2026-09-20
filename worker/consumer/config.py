"""Consumer configuration. Everything that differs between local and AWS."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class ConsumerConfig:
    queue_url: str = os.environ.get("SQS_QUEUE_URL", "")
    s3_bucket: str = os.environ.get("S3_BUCKET", "docintel-local")
    report_prefix: str = os.environ.get("REPORT_PREFIX", "reports/")
    upload_prefix: str = os.environ.get("UPLOAD_PREFIX", "uploads/")

    aws_region: str = os.environ.get("AWS_REGION", "ap-southeast-1")
    # Set locally to point boto3 at LocalStack. Unset on AWS.
    aws_endpoint_url: str | None = os.environ.get("AWS_ENDPOINT_URL")

    database_dsn: str = os.environ.get("DATABASE_DSN", "")
    db_host: str = os.environ.get("DB_HOST", "localhost")
    db_port: int = _int("DB_PORT", 5432)
    db_name: str = os.environ.get("DB_NAME", "docintel")
    db_user: str = os.environ.get("DB_USER", "docintel")
    db_secret_arn: str | None = os.environ.get("DB_SECRET_ARN")

    # Long polling. 20 is the SQS maximum and the point of it: one request that waits,
    # rather than many that return empty.
    wait_time_seconds: int = _int("SQS_WAIT_TIME_SECONDS", 20)
    # One message at a time. The graph is the slow part, so batching would only mean holding
    # several documents hostage to the slowest one in the batch.
    max_messages: int = 1

    # How far ahead the lease and the SQS visibility timeout are pushed on each heartbeat.
    # One number for both, so the two can never disagree about who owns the document.
    lease_seconds: int = _int("LEASE_SECONDS", 120)
    # How often to push them. Must be comfortably less than lease_seconds, or a slow node
    # lets the lease lapse while the worker is still alive and working.
    heartbeat_seconds: int = _int("HEARTBEAT_SECONDS", 30)

    # The number of receives after which SQS sends the message to the dead letter queue.
    # The consumer needs to know this to recognise its own last attempt.
    max_receive_count: int = _int("MAX_RECEIVE_COUNT", 3)

    # Reaper. The grace period must outlast the whole redrive window, so that by the time a
    # row is swept, SQS has already given up on its message.
    reap_interval_seconds: int = _int("REAP_INTERVAL_SECONDS", 300)
    processing_grace_seconds: int = _int("PROCESSING_GRACE_SECONDS", 900)
    # Matches the presigned POST expiry in the API, plus a margin for a slow upload.
    upload_expiry_seconds: int = _int("UPLOAD_EXPIRY_SECONDS", 900)

    shutdown_grace_seconds: int = _int("SHUTDOWN_GRACE_SECONDS", 110)
    heartbeat_file: str = os.environ.get("HEARTBEAT_FILE", "/tmp/worker-heartbeat")


CONSUMER_CONFIG = ConsumerConfig()
