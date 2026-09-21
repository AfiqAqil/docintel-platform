"""The claim and the reaper, against a real PostgreSQL.

These are not unit tests. Every statement here depends on PostgreSQL semantics: now(),
make_interval, ANY against an array, and what a conditional UPDATE does when two
transactions race. A fake or an in memory database would prove nothing about the thing that
has to be correct.

The schema is created by running the backend's own Alembic migration, so the SQL under test
runs against the real table definition rather than a copy that can drift from it.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from consumer.claim import (
    Claim,
    ClaimRefused,
    claim,
    heartbeat,
    mark_completed,
    mark_failed,
)
from consumer.reaper import reap

DSN = os.environ.get(
    "TEST_DATABASE_DSN", "postgresql://docintel:docintel@localhost:55432/docintel"
)

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(database_schema: None):
    connection = psycopg.connect(DSN)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE documents")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def other_conn():
    """A second connection, for the races. One connection cannot race itself."""
    connection = psycopg.connect(DSN)
    yield connection
    connection.close()


def insert(
    conn: psycopg.Connection,
    status: str = "QUEUED",
    lease_offset_seconds: int | None = None,
    created_offset_seconds: int = 0,
) -> uuid.UUID:
    document_id = uuid.uuid4()
    lease = (
        datetime.now(UTC) + timedelta(seconds=lease_offset_seconds)
        if lease_offset_seconds is not None
        else None
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents
                (id, filename, content_type, size_bytes, status, lease_expires_at, created_at)
            VALUES (%s, 'sample.pdf', 'application/pdf', 1024, %s, %s,
                    now() - make_interval(secs => %s))
            """,
            (str(document_id), status, lease, created_offset_seconds),
        )
    conn.commit()
    return document_id


def status_of(conn: psycopg.Connection, document_id: uuid.UUID) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM documents WHERE id = %s", (str(document_id),))
        row = cur.fetchone()
    # Every caller has just inserted or claimed this row, so a miss is a broken test rather
    # than a case to handle. Failing here names the problem instead of raising a
    # TypeError three lines later.
    assert row is not None, f"no row for {document_id}"
    return row


# ---------------------------------------------------------------------------------------
# What can be claimed
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["QUEUED", "UPLOADING", "EXPIRED"])
def test_claimable_statuses_can_be_claimed(conn, status):
    """UPLOADING because the S3 event can beat the browser's upload-complete call.

    EXPIRED because the reaper's guess that a user walked away is disproved by the arrival
    of an event: the object exists.
    """
    document_id = insert(conn, status=status)

    result = claim(conn, document_id, lease_seconds=60)

    assert isinstance(result, Claim)
    assert status_of(conn, document_id)["status"] == "PROCESSING"


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
def test_finished_documents_are_never_reclaimed(conn, status):
    """The most important negative case in this file.

    A finished document's lease is NULL or long past, so the expired lease clause would
    match it if the claimable statuses were not named explicitly. Without this, every
    finished document would be reprocessable forever on any redelivery.
    """
    document_id = insert(conn, status=status, lease_offset_seconds=-86400)

    result = claim(conn, document_id, lease_seconds=60)

    assert isinstance(result, ClaimRefused)
    assert result.terminal is True
    assert status_of(conn, document_id)["status"] == status


def test_a_live_lease_blocks_a_second_claim(conn):
    """A document someone else is actively working on is not available."""
    document_id = insert(conn, status="PROCESSING", lease_offset_seconds=300)

    result = claim(conn, document_id, lease_seconds=60)

    assert isinstance(result, ClaimRefused)
    assert result.terminal is False
    assert result.status == "PROCESSING"


def test_an_expired_lease_is_reclaimable(conn):
    """Recovery after a worker dies mid graph: the lease lapses and the work is available."""
    document_id = insert(conn, status="PROCESSING", lease_offset_seconds=-1)

    result = claim(conn, document_id, lease_seconds=60)

    assert isinstance(result, Claim)
    # attempt_count is how a later delivery can tell a retry from a first try.
    assert result.attempt_count == 1


def test_a_missing_row_is_reported_as_missing_not_terminal(conn):
    """An object in the bucket with no row. Not finished, so the message must not be acked
    on the terminal rule; the caller decides separately."""
    result = claim(conn, uuid.uuid4(), lease_seconds=60)

    assert isinstance(result, ClaimRefused)
    assert result.missing is True
    assert result.terminal is False


def test_two_workers_racing_for_the_same_document_produce_exactly_one_winner(conn, other_conn):
    """The whole at-least-once design rests on this.

    Both transactions run the same statement against the same row. The second one's WHERE no
    longer matches once the first commits, so it cannot also win.
    """
    document_id = insert(conn, status="QUEUED")

    first = claim(conn, document_id, lease_seconds=60)
    second = claim(other_conn, document_id, lease_seconds=60)

    assert isinstance(first, Claim)
    assert isinstance(second, ClaimRefused)
    assert second.terminal is False
    assert status_of(conn, document_id)["attempt_count"] == 1


# ---------------------------------------------------------------------------------------
# The lease, and who is allowed to write a result
# ---------------------------------------------------------------------------------------


def test_heartbeat_extends_the_lease_and_records_the_step(conn):
    document_id = insert(conn, status="QUEUED")
    claimed = claim(conn, document_id, lease_seconds=2)
    before = status_of(conn, document_id)["lease_expires_at"]

    assert (
        heartbeat(
            conn,
            document_id,
            lease_seconds=600,
            attempt_count=claimed.attempt_count,
            current_step="classify",
        )
        is True
    )

    after = status_of(conn, document_id)
    assert after["lease_expires_at"] > before
    assert after["current_step"] == "classify"


def test_heartbeat_fails_once_the_lease_has_been_taken(conn, other_conn):
    """Tells a worker it has been superseded, so it stops rather than finishing over the
    top of whoever owns the document now."""
    document_id = insert(conn, status="PROCESSING", lease_offset_seconds=-1)
    stale_attempt = status_of(conn, document_id)["attempt_count"]
    assert isinstance(claim(other_conn, document_id, lease_seconds=300), Claim)

    # The original worker carries the token from before the reclaim, so its beat is refused
    # even though the row is PROCESSING with a live lease, which is the whole point.
    assert heartbeat(conn, document_id, lease_seconds=60, attempt_count=stale_attempt) is False


def test_a_worker_that_lost_its_lease_cannot_write_a_result(conn):
    """Without the lease condition on the write, a slow worker finishing late would
    overwrite the result of the worker that took the document from it."""
    document_id = insert(conn, status="PROCESSING", lease_offset_seconds=-1)
    attempt = status_of(conn, document_id)["attempt_count"]

    assert (
        mark_completed(conn, document_id, "COMPLETE", "claim_form", None, attempt_count=attempt)
        is False
    )
    assert mark_failed(conn, document_id, "boom", attempt_count=attempt) is False
    assert status_of(conn, document_id)["status"] == "PROCESSING"


def test_the_holder_of_the_lease_can_complete_and_fail(conn):
    document_id = insert(conn, status="QUEUED")
    claimed = claim(conn, document_id, lease_seconds=300)

    assert mark_completed(
        conn,
        document_id,
        "INCOMPLETE",
        "claim_form",
        '{"summary": "x"}',
        attempt_count=claimed.attempt_count,
    )

    row = status_of(conn, document_id)
    assert row["status"] == "COMPLETED"
    assert row["outcome"] == "INCOMPLETE"
    assert row["lease_expires_at"] is None
    assert row["completed_at"] is not None


def test_failing_leaves_no_outcome(conn):
    """The database refuses an outcome on a row that is not COMPLETED, so a failure that
    tried to carry one would be rejected rather than stored."""
    document_id = insert(conn, status="QUEUED")
    claimed = claim(conn, document_id, lease_seconds=300)

    assert mark_failed(
        conn,
        document_id,
        "PDF is encrypted",
        attempt_count=claimed.attempt_count,
        doc_type="unknown",
    )

    row = status_of(conn, document_id)
    assert row["status"] == "FAILED"
    assert row["outcome"] is None
    assert "encrypted" in row["error_message"]


# ---------------------------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------------------------


def test_reaper_fails_rows_stuck_in_processing_past_the_grace_period(conn):
    """A worker that died on every attempt sends the message to the DLQ with nobody writing
    a status. Nothing else will ever finish this row."""
    document_id = insert(conn, status="PROCESSING", lease_offset_seconds=-3600)

    result = reap(conn, processing_grace_seconds=600, upload_expiry_seconds=900)

    assert result.stuck_processing == 1
    row = status_of(conn, document_id)
    assert row["status"] == "FAILED"
    assert row["lease_expires_at"] is None


def test_reaper_leaves_a_recently_expired_lease_alone(conn):
    """A lease that lapsed a moment ago probably belongs to a worker whose heartbeat is
    briefly late. Redelivery should reclaim it; the reaper declaring it dead would race
    with the worker that is about to pick it up."""
    document_id = insert(conn, status="PROCESSING", lease_offset_seconds=-5)

    result = reap(conn, processing_grace_seconds=600, upload_expiry_seconds=900)

    assert result.stuck_processing == 0
    assert status_of(conn, document_id)["status"] == "PROCESSING"


def test_reaper_expires_abandoned_uploads(conn):
    """A user asked for an upload slot and never used it, so no object exists and no event
    will ever arrive."""
    document_id = insert(conn, status="UPLOADING", created_offset_seconds=3600)

    result = reap(conn, processing_grace_seconds=600, upload_expiry_seconds=900)

    assert result.abandoned_uploads == 1
    assert status_of(conn, document_id)["status"] == "EXPIRED"


def test_reaper_leaves_a_fresh_upload_alone(conn):
    """The user may still be uploading."""
    document_id = insert(conn, status="UPLOADING", created_offset_seconds=10)

    assert reap(conn, processing_grace_seconds=600, upload_expiry_seconds=900).total == 0
    assert status_of(conn, document_id)["status"] == "UPLOADING"


def test_an_expired_document_still_processes_when_its_event_finally_arrives(conn):
    """EXPIRED is a guess, and an S3 event disproves it.

    This is the end to end version of why EXPIRED is not terminal: a document swept while
    its message sat in a backed up queue must still process when the message is delivered.
    """
    document_id = insert(conn, status="UPLOADING", created_offset_seconds=3600)
    reap(conn, processing_grace_seconds=600, upload_expiry_seconds=900)
    assert status_of(conn, document_id)["status"] == "EXPIRED"

    result = claim(conn, document_id, lease_seconds=60)

    assert isinstance(result, Claim)
    assert status_of(conn, document_id)["status"] == "PROCESSING"


def test_reaper_does_not_touch_finished_documents(conn):
    for status in ("COMPLETED", "FAILED"):
        insert(conn, status=status, lease_offset_seconds=-86400, created_offset_seconds=86400)

    assert reap(conn, processing_grace_seconds=600, upload_expiry_seconds=900).total == 0


def test_a_superseded_worker_cannot_write_after_the_document_is_reclaimed(conn, other_conn):
    """The fencing test. Holding a lease is not the same as holding THIS lease.

    Sequence, all of it legal:
      1. A claims with a short lease.
      2. A stalls long enough for the lease to lapse.
      3. B reclaims, which is correct: an expired lease is available.
      4. A wakes up and beats, which pushes the lease out again.
      5. A writes its result.

    At step 5 the row is PROCESSING with a live lease, so a check for those two things alone
    passes, and A overwrites B's document while B is still working on it. The statements need
    to know not just that a lease is held, but by whom.
    """
    document_id = insert(conn, status="QUEUED")

    a = claim(conn, document_id, lease_seconds=1)
    assert isinstance(a, Claim)

    # The lease lapses.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (str(document_id),),
        )
    conn.commit()

    b = claim(other_conn, document_id, lease_seconds=300)
    assert isinstance(b, Claim)
    assert b.attempt_count == a.attempt_count + 1, "B is a later attempt than A"

    # A wakes up. Its heartbeat must not succeed, because the document is not A's any more.
    assert heartbeat(conn, document_id, lease_seconds=300, attempt_count=a.attempt_count) is False

    # And A's result must not land on top of B's document.
    assert (
        mark_completed(
            conn, document_id, "COMPLETE", "claim_form", None, attempt_count=a.attempt_count
        )
        is False
    )
    assert (
        mark_failed(conn, document_id, "stale failure", attempt_count=a.attempt_count) is False
    )

    row = status_of(conn, document_id)
    assert row["status"] == "PROCESSING", "B still owns it"
    assert row["attempt_count"] == b.attempt_count

    # B, which does hold the current attempt, can still finish normally.
    assert mark_completed(
        other_conn, document_id, "COMPLETE", "claim_form", None, attempt_count=b.attempt_count
    )
    assert status_of(conn, document_id)["status"] == "COMPLETED"


def test_progress_is_recorded_per_node_and_fenced(conn, other_conn):
    """current_step is written as each node finishes, not on the heartbeat's own clock.

    Deferring it to the heartbeat meant it was almost always null, because the beat runs
    every 30 seconds and most documents finish well inside that, so the progress the frontend
    promises never appeared.
    """
    from consumer.claim import record_step

    document_id = insert(conn, status="QUEUED")
    claimed = claim(conn, document_id, lease_seconds=300)

    record_step(conn, document_id, claimed.attempt_count, "classify")
    assert status_of(conn, document_id)["current_step"] == "classify"

    record_step(conn, document_id, claimed.attempt_count, "extract_claim")
    assert status_of(conn, document_id)["current_step"] == "extract_claim"

    # Writing progress must not renew the lease, or a worker stuck in a slow node would keep
    # its own claim alive purely by reporting on it.
    before = status_of(conn, document_id)["lease_expires_at"]
    record_step(conn, document_id, claimed.attempt_count, "validate")
    assert status_of(conn, document_id)["lease_expires_at"] == before

    # And a superseded worker cannot report progress onto a document it no longer owns.
    stale = claimed.attempt_count
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (str(document_id),),
        )
    conn.commit()
    assert isinstance(claim(other_conn, document_id, lease_seconds=300), Claim)

    record_step(conn, document_id, stale, "generate_report")
    assert status_of(conn, document_id)["current_step"] != "generate_report"


def test_an_uncommitted_completion_locks_the_row_against_a_second_claim(conn, other_conn):
    """The row lock is what makes the status write and the report publication one step.

    The consumer holds COMPLETE_SQL open while it writes the report to S3, so that a failed
    put can roll the status back. That is only safe if nobody else can take the document in
    the meantime.

    For most of that window nothing has to block: the lease is live, so a second claim does
    not match the WHERE at all and is refused outright. The case that needs the lock is the
    narrow one where the lease lapses after the status write and before the put finishes.
    Then the second claim does match, and it must wait rather than overtake, which is what
    the short lock_timeout below turns into a visible failure instead of a silent race.
    """
    document_id = insert(conn, status="QUEUED")
    # A one second lease, so it can be allowed to lapse inside the test without a long wait.
    claimed = claim(conn, document_id, lease_seconds=1)
    assert isinstance(claimed, Claim)

    # The consumer's window opens: the status is written, still holding the lease, and not
    # yet committed because the report has not been stored.
    assert mark_completed(
        conn, document_id, "COMPLETE", "claim_form", None, claimed.attempt_count, commit=False
    )

    # The put is slow, and the lease lapses while it runs. A second worker now matches the
    # expired lease clause, so only the row lock stands between it and the document.
    time.sleep(1.2)

    with other_conn.cursor() as cur:
        cur.execute("SET lock_timeout = '500ms'")
    with pytest.raises(psycopg.errors.LockNotAvailable):
        claim(other_conn, document_id, lease_seconds=60)
    other_conn.rollback()

    # Once the consumer commits, the second worker sees a finished document rather than a
    # claimable one, so it refuses for the right reason.
    conn.commit()
    refused = claim(other_conn, document_id, lease_seconds=60)
    assert isinstance(refused, ClaimRefused)
    assert refused.terminal


def test_a_rolled_back_completion_leaves_the_document_claimable_again(conn, other_conn):
    """The other half: when the report cannot be stored, the document must come back.

    Rolling back releases the lock and restores PROCESSING with the lease the claim set, so
    the redelivered message can be claimed again once that lease lapses.
    """
    document_id = insert(conn, status="QUEUED")
    claimed = claim(conn, document_id, lease_seconds=60)
    assert isinstance(claimed, Claim)

    assert mark_completed(
        conn, document_id, "COMPLETE", "claim_form", None, claimed.attempt_count, commit=False
    )
    conn.rollback()

    row = status_of(conn, document_id)
    assert row["status"] == "PROCESSING"
    assert row["outcome"] is None, "the rollback must undo the outcome too"

    # The lease the claim set is still live, so the redelivery only succeeds once it lapses.
    # Expiring it here is what waiting out the visibility timeout does in production.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (str(document_id),),
        )
    conn.commit()

    reclaimed = claim(other_conn, document_id, lease_seconds=60)
    assert isinstance(reclaimed, Claim)
    assert reclaimed.attempt_count == 2
