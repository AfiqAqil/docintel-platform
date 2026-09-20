"""Claiming a document, and finishing it. The heart of the at-least-once story.

SQS is at-least-once, so the same document can arrive more than once: a redelivery after a
visibility timeout, a duplicate S3 notification, two workers racing on a backlog. Nothing
here assumes a message is unique. Correctness comes from the database, where exactly one
conditional UPDATE can win.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

# Statuses a document can be claimed from.
#
# UPLOADING is here because the S3 event can beat the browser's upload-complete call, so a
# document can legitimately still be UPLOADING when its object already exists.
#
# EXPIRED is here because the reaper's second sweep is a guess about a user who walked away,
# and an S3 event is proof that guess was wrong: the object exists. A document swept while
# its message sat in a backed up queue must still process.
#
# COMPLETED and FAILED are deliberately absent. Their leases are long expired too, so
# without naming the claimable statuses explicitly, the expired lease clause below would
# make every finished document reprocessable forever.
CLAIMABLE = ("UPLOADING", "QUEUED", "EXPIRED")

CLAIM_SQL = """
UPDATE documents
   SET status           = 'PROCESSING',
       lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
       attempt_count    = attempt_count + 1,
       current_step     = NULL,
       error_message    = NULL,
       updated_at       = now()
 WHERE id = %(document_id)s
   AND (
         status = ANY(%(claimable)s)
      OR (status = 'PROCESSING' AND lease_expires_at < now())
       )
RETURNING id, status, attempt_count, lease_expires_at
"""

# Read the row when the claim matched nothing, so the caller can tell "someone else owns it"
# from "it is already finished". Those need opposite handling, and conflating them either
# deletes work that is still in flight or loops on a document that is done.
PEEK_SQL = "SELECT id, status, lease_expires_at FROM documents WHERE id = %(document_id)s"

# attempt_count is the fencing token, and it is the reason these statements are correct.
#
# Checking that the row is PROCESSING with a live lease says a lease is held. It does not say
# who holds it, and that distinction is not academic: a worker whose lease lapsed, whose
# document was reclaimed, and which then woke up and beat, would push the lease out again and
# pass exactly that check while another worker owned the document.
#
# The claim increments attempt_count and returns it, so every worker carries the number its
# own claim produced. A reclaim bumps it, which invalidates the previous holder's token
# permanently. There is no window in which two workers hold the same one.
HEARTBEAT_SQL = """
UPDATE documents
   SET lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
       current_step     = COALESCE(%(current_step)s, current_step),
       updated_at       = now()
 WHERE id = %(document_id)s
   AND status = 'PROCESSING'
   AND lease_expires_at > now()
   AND attempt_count = %(attempt_count)s
RETURNING lease_expires_at
"""

# Every write that finishes a document is conditional on still holding this attempt, not
# merely on a lease being live. See the note above HEARTBEAT_SQL.
COMPLETE_SQL = """
UPDATE documents
   SET status           = 'COMPLETED',
       outcome          = %(outcome)s,
       doc_type         = %(doc_type)s,
       report_summary   = %(report_summary)s,
       current_step     = NULL,
       error_message    = NULL,
       lease_expires_at = NULL,
       completed_at     = now(),
       updated_at       = now()
 WHERE id = %(document_id)s
   AND status = 'PROCESSING'
   AND lease_expires_at > now()
   AND attempt_count = %(attempt_count)s
RETURNING id
"""

FAIL_SQL = """
UPDATE documents
   SET status           = 'FAILED',
       outcome          = NULL,
       doc_type         = %(doc_type)s,
       current_step     = NULL,
       error_message    = %(error_message)s,
       lease_expires_at = NULL,
       completed_at     = now(),
       updated_at       = now()
 WHERE id = %(document_id)s
   AND status = 'PROCESSING'
   AND lease_expires_at > now()
   AND attempt_count = %(attempt_count)s
RETURNING id
"""


# Progress only. Deliberately not the heartbeat statement: writing a step should never
# extend a lease as a side effect, or a worker stuck in a slow node would keep renewing its
# own claim by making progress reports about it.
STEP_SQL = """
UPDATE documents
   SET current_step = %(current_step)s,
       updated_at   = now()
 WHERE id = %(document_id)s
   AND status = 'PROCESSING'
   AND attempt_count = %(attempt_count)s
"""


@dataclass(frozen=True)
class Claim:
    document_id: uuid.UUID
    attempt_count: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class ClaimRefused:
    """Why a claim matched no row, which decides whether the message may be acked."""

    status: str | None
    #: True when the document is COMPLETED or FAILED. Only then is deleting the message safe.
    terminal: bool
    #: True when the row does not exist at all.
    missing: bool


def claim(
    conn: psycopg.Connection, document_id: uuid.UUID, lease_seconds: int
) -> Claim | ClaimRefused:
    """Take ownership of a document, or explain why not.

    One statement decides. Two workers running this concurrently cannot both win, because
    the loser's WHERE no longer matches once the winner commits.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            CLAIM_SQL,
            {
                "document_id": str(document_id),
                "lease_seconds": lease_seconds,
                "claimable": list(CLAIMABLE),
            },
        )
        row = cur.fetchone()
        if row is not None:
            conn.commit()
            return Claim(
                document_id=row["id"],
                attempt_count=row["attempt_count"],
                lease_expires_at=row["lease_expires_at"],
            )

        # Nothing matched. Find out why before deciding what to do with the message.
        cur.execute(PEEK_SQL, {"document_id": str(document_id)})
        existing = cur.fetchone()

    conn.commit()

    if existing is None:
        # An S3 object with no database row: either the row was deleted, or something other
        # than our own intake put an object into the bucket.
        return ClaimRefused(status=None, terminal=False, missing=True)

    return ClaimRefused(
        status=existing["status"],
        terminal=existing["status"] in ("COMPLETED", "FAILED"),
        missing=False,
    )


def heartbeat(
    conn: psycopg.Connection,
    document_id: uuid.UUID,
    lease_seconds: int,
    attempt_count: int,
    current_step: str | None = None,
) -> bool:
    """Extend the lease, and record which node is running.

    `attempt_count` is the token this worker's own claim returned. Returns False when it is
    no longer the current one, which tells the caller to stop: someone else owns the document
    now, and finishing would overwrite their work.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            HEARTBEAT_SQL,
            {
                "document_id": str(document_id),
                "lease_seconds": lease_seconds,
                "current_step": current_step,
                "attempt_count": attempt_count,
            },
        )
        held = cur.fetchone() is not None
    conn.commit()
    return held


def record_step(
    conn: psycopg.Connection, document_id: uuid.UUID, attempt_count: int, node: str
) -> None:
    """Record which node just finished.

    Called once per node so the frontend can show progress while a document is processing,
    which is what architecture section 4 asks for. Fenced like every other write, so a
    superseded worker cannot report progress onto a document it no longer owns.

    Failures are swallowed: this is a progress indicator, and losing one is not a reason to
    abandon a document that is otherwise processing correctly.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                STEP_SQL,
                {
                    "document_id": str(document_id),
                    "attempt_count": attempt_count,
                    "current_step": node,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()


def _finish(
    conn: psycopg.Connection, sql: str, params: dict[str, Any], commit: bool = True
) -> bool:
    """Run one fenced finishing write.

    With commit=False the transaction is left open when the write matched, so the caller can
    make something else durable before the new status becomes visible to anyone. The row lock
    the UPDATE took is held until the caller commits or rolls back, which is what stops
    another worker claiming the document during that window.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        written = cur.fetchone() is not None
    if not written:
        # Nothing matched, so there is nothing to hold open either way.
        conn.rollback()
        return False
    if commit:
        conn.commit()
    return True


def mark_completed(
    conn: psycopg.Connection,
    document_id: uuid.UUID,
    outcome: str,
    doc_type: str | None,
    report_summary: str | None,
    attempt_count: int,
    commit: bool = True,
) -> bool:
    """Record a successful run.

    False means this worker no longer holds the attempt it claimed, so nothing was written.
    """
    return _finish(
        conn,
        COMPLETE_SQL,
        {
            "document_id": str(document_id),
            "outcome": outcome,
            "doc_type": doc_type,
            "report_summary": report_summary,
            "attempt_count": attempt_count,
        },
        commit=commit,
    )


def mark_failed(
    conn: psycopg.Connection,
    document_id: uuid.UUID,
    error_message: str,
    attempt_count: int,
    doc_type: str | None = None,
    commit: bool = True,
) -> bool:
    """Record a terminal failure. Only the worker holding the current attempt may write it."""
    return _finish(
        conn,
        FAIL_SQL,
        {
            "document_id": str(document_id),
            "error_message": error_message[:2048],
            "doc_type": doc_type,
            "attempt_count": attempt_count,
        },
        commit=commit,
    )
