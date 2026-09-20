"""The database schema. One table, owned here, defined once.

The worker does not import this module: it talks to the same table in raw SQL, because its
whole interaction is one conditional UPDATE to claim a document and two to sweep stale rows,
and an ORM would hide exactly the statements that have to be correct. What the worker's tests
do use is this migration, run against a real PostgreSQL, so the SQL under test runs against
the real table rather than a hand written copy that can drift.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Status(enum.StrEnum):
    """Where a document is. Answers "where is it", not "how did it go"."""

    UPLOADING = "UPLOADING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    # Requested an upload slot and never used it. Not terminal: an S3 event is proof the
    # object exists, so a document swept while its message sat in a backed up queue is
    # claimable again.
    EXPIRED = "EXPIRED"


class Outcome(enum.StrEnum):
    """How processing went, for a document that reached COMPLETED."""

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNSUPPORTED = "UNSUPPORTED"


#: Statuses a document can be claimed from. COMPLETED and FAILED are absent on purpose: their
#: leases are long expired too, and without this list an expired lease would make a finished
#: document reprocessable forever.
CLAIMABLE_STATUSES = (Status.UPLOADING, Status.QUEUED, Status.EXPIRED)

TERMINAL_STATUSES = (Status.COMPLETED, Status.FAILED)


class Document(Base):
    __tablename__ = "documents"

    # Also the S3 key suffix and the correlation id in every log line across all three
    # services. One identifier, so a document can be followed end to end without a join.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    # The original filename lives here rather than in the S3 key. S3 URL encodes keys in
    # event notifications, so a filename with a space would arrive as a "+" and a consumer
    # would look for an object that does not exist.
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The graph node currently running, written by the consumer as the graph streams.
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Who owns this document and until when. The claim and both reaper sweeps key off it.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The summary only. The full report is reports/{id}.json in S3, because a report grows
    # with the document and a database row should not.
    report_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Both columns drive routing in the worker and rendering in the frontend, so an
        # unexpected value should fail at the write rather than become a row that quietly
        # breaks a screen later. Written as CHECK rather than a PostgreSQL enum type because
        # adding a value to an enum type is a migration with a lock, while a CHECK is a
        # constraint swap.
        CheckConstraint(
            "status IN ('UPLOADING','QUEUED','PROCESSING','COMPLETED','FAILED','EXPIRED')",
            name="ck_documents_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('COMPLETE','INCOMPLETE','UNSUPPORTED')",
            name="ck_documents_outcome",
        ),
        # Validation at the edge is not a guarantee about the table. Anything writing here
        # that is not the API, a migration, a fix applied by hand, a future service, is not
        # covered by a Pydantic model, and a negative size is impossible rather than merely
        # unwanted.
        CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        # An outcome only means anything once processing finished. Without this a row can
        # claim to be QUEUED and COMPLETE at the same time.
        CheckConstraint(
            "outcome IS NULL OR status = 'COMPLETED'",
            name="ck_documents_outcome_requires_completed",
        ),
        # Serves the claim and both reaper sweeps, which all filter on status and compare
        # the lease against now().
        Index("ix_documents_status_lease", "status", "lease_expires_at"),
        # Serves the history list, which is the only query the API makes at any volume.
        Index("ix_documents_created_at", created_at.desc()),
    )
