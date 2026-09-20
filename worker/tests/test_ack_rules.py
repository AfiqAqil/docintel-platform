"""The ack decision, exercised through the real poll loop against a real database.

Acking is the one irreversible thing this service does. Deleting a message destroys the only
copy of that work, so every path that deletes one has to be provably finished, and every
path that is merely unclear has to leave it alone.

SQS and S3 are stubbed here because neither has any decision in them. The database is real,
because every decision below is made by the claim statement.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

DSN = os.environ.get(
    "TEST_DATABASE_DSN", "postgresql://docintel:docintel@localhost:55432/docintel"
)

pytestmark = pytest.mark.integration


@pytest.fixture
def conn():
    connection = psycopg.connect(DSN)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE documents")
    connection.commit()
    yield connection
    connection.close()


class FakeSQS:
    """Records what the worker did with each message, which is the whole assertion."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages
        self.deleted: list[str] = []
        self.visibility_calls: list[int] = []

    def receive_message(self, **_: Any) -> dict[str, Any]:
        if not self._messages:
            return {}
        return {"Messages": [self._messages.pop(0)]}

    def delete_message(self, ReceiptHandle: str, **_: Any) -> None:
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, VisibilityTimeout: int, **_: Any) -> None:
        self.visibility_calls.append(VisibilityTimeout)


class FakeS3:
    def __init__(self, body: bytes = b"", fail: bool = False, fail_put: bool = False) -> None:
        self._body = body
        self._fail = fail
        self._fail_put = fail_put
        self.puts: list[str] = []

    def get_object(self, **_: Any) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError("S3 is unavailable")

        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"Body": _Body(self._body)}

    def head_object(self, **_: Any) -> dict[str, Any]:
        return {"ContentType": "application/pdf"}

    def put_object(self, Key: str, **_: Any) -> None:
        if self._fail_put:
            raise RuntimeError("S3 is unavailable")
        self.puts.append(Key)


def make_worker(monkeypatch, sqs: FakeSQS, s3: FakeS3):
    """Build a Worker with its AWS clients replaced and its graph left real."""
    from consumer import main as consumer_main

    monkeypatch.setattr(consumer_main, "_boto", lambda service: sqs if service == "sqs" else s3)
    monkeypatch.setattr(consumer_main, "connect", lambda: psycopg.connect(DSN))
    worker = consumer_main.Worker()
    worker.sqs = sqs
    worker.s3 = s3
    return worker


def sqs_visibility(sqs: FakeSQS):
    """The extender the poll loop builds from the receipt handle, for a direct _record call."""
    return lambda seconds: sqs.change_message_visibility(VisibilityTimeout=seconds)


def message(document_id: uuid.UUID, receipt: str = "r1", receive_count: int = 1) -> dict:
    return {
        "ReceiptHandle": receipt,
        "Attributes": {"ApproximateReceiveCount": str(receive_count)},
        "Body": json.dumps(
            {
                "Records": [
                    {
                        "s3": {
                            "bucket": {"name": "docintel"},
                            "object": {"key": f"uploads/{document_id}", "size": 10},
                        }
                    }
                ]
            }
        ),
    }


def insert(conn, status: str, lease_offset: int | None = None) -> uuid.UUID:
    document_id = uuid.uuid4()
    lease = (
        datetime.now(UTC) + timedelta(seconds=lease_offset)
        if lease_offset is not None
        else None
    )
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (id, filename, content_type, size_bytes, status,
                                      lease_expires_at)
               VALUES (%s,'s.pdf','application/pdf',10,%s,%s)""",
            (str(document_id), status, lease),
        )
    conn.commit()
    return document_id


def status_of(conn, document_id: uuid.UUID) -> str:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM documents WHERE id = %s", (str(document_id),))
        row = cur.fetchone()
        return row["status"] if row else "GONE"


# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
def test_a_finished_document_deletes_the_message(conn, monkeypatch, status):
    """Terminal means COMPLETED or FAILED, and only then is deleting safe.

    A redelivery of an already finished document is the common case after a slow ack, and
    leaving the message would make it loop until the redrive limit.
    """
    document_id = insert(conn, status, lease_offset=-3600)
    sqs = FakeSQS([message(document_id)])
    worker = make_worker(monkeypatch, sqs, FakeS3())

    worker._handle(sqs._messages.pop(0) if sqs._messages else message(document_id))

    assert sqs.deleted == ["r1"]
    assert status_of(conn, document_id) == status


def test_a_document_held_by_another_worker_returns_the_message(conn, monkeypatch):
    """The most important negative case in this file.

    Another worker holds a live lease. Deleting the message here would destroy the only copy
    of the work, and if that worker then died the document would never be processed by
    anyone. So it is left to time out and come back.
    """
    document_id = insert(conn, "PROCESSING", lease_offset=600)
    sqs = FakeSQS([])
    worker = make_worker(monkeypatch, sqs, FakeS3())

    worker._handle(message(document_id))

    assert sqs.deleted == []
    assert status_of(conn, document_id) == "PROCESSING"


def test_an_object_with_no_database_row_deletes_the_message(conn, monkeypatch):
    """No amount of redelivery will create the row, so returning it just burns attempts
    until it reaches the dead letter queue."""
    sqs = FakeSQS([])
    worker = make_worker(monkeypatch, sqs, FakeS3())

    worker._handle(message(uuid.uuid4()))

    assert sqs.deleted == ["r1"]


def test_an_unparseable_message_is_deleted(conn, monkeypatch):
    """Redelivering it cannot change the outcome, and leaving it blocks the queue for the
    length of the redrive window on every delivery."""
    sqs = FakeSQS([])
    worker = make_worker(monkeypatch, sqs, FakeS3())

    worker._handle({"ReceiptHandle": "r1", "Attributes": {}, "Body": "{}"})

    assert sqs.deleted == ["r1"]


def test_the_s3_test_event_is_deleted_without_touching_the_database(conn, monkeypatch):
    sqs = FakeSQS([])
    worker = make_worker(monkeypatch, sqs, FakeS3())

    worker._handle(
        {
            "ReceiptHandle": "r1",
            "Attributes": {},
            "Body": json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"}),
        }
    )

    assert sqs.deleted == ["r1"]


def test_a_fetch_failure_returns_the_message_when_attempts_remain(conn, monkeypatch):
    """S3 being briefly unavailable is not the document's fault. The document must not be
    marked failed while it is still going to be retried."""
    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    worker = make_worker(monkeypatch, sqs, FakeS3(fail=True))

    worker._handle(message(document_id, receive_count=1))

    assert sqs.deleted == []
    assert status_of(conn, document_id) == "PROCESSING"


def test_a_fetch_failure_on_the_last_attempt_records_failure_and_deletes(conn, monkeypatch):
    """On the final delivery there is no further retry coming, so the document must be
    recorded as failed rather than left in PROCESSING for the reaper to find."""
    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    worker = make_worker(monkeypatch, sqs, FakeS3(fail=True))

    worker._handle(message(document_id, receive_count=3))

    assert sqs.deleted == ["r1"]
    assert status_of(conn, document_id) == "FAILED"


def test_a_worker_that_lost_its_lease_writes_nothing_at_all(conn, monkeypatch):
    """Ownership is settled by the database write, so nothing may be published before it.

    Publishing the report first meant a worker that had already lost the lease still
    overwrote the winner's report, and only then discovered its own database write was
    refused. The row would describe one run while the stored report came from another, with
    no error raised anywhere. A wrong answer nobody can detect is worse than a missing one.
    """
    from consumer import main as consumer_main

    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    s3 = FakeS3()
    worker = make_worker(monkeypatch, sqs, s3)

    # The lease is lost between the last heartbeat and the final write, which is exactly
    # the window the conditional write exists to cover.
    monkeypatch.setattr(consumer_main, "mark_completed", lambda *a, **k: False)
    monkeypatch.setattr(consumer_main, "mark_failed", lambda *a, **k: False)

    decision = worker._record(
        conn,
        type("E", (), {"document_id": document_id})(),
        {"document_type": "claim_form", "outcome": "COMPLETE", "summary": "s"},
        {"outcome": "COMPLETE"},
        type("C", (), {"attempt_count": 1})(),
        sqs_visibility(sqs),
    )

    assert decision is consumer_main.Ack.RETURN
    assert s3.puts == [], "a worker without the lease must not publish a report"


def test_the_holder_of_the_lease_publishes_exactly_one_report(conn, monkeypatch):
    """The other half of the rule: once the write succeeds, the report is published."""
    from consumer import main as consumer_main

    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    s3 = FakeS3()
    worker = make_worker(monkeypatch, sqs, s3)
    monkeypatch.setattr(consumer_main, "mark_completed", lambda *a, **k: True)

    decision = worker._record(
        conn,
        type("E", (), {"document_id": document_id})(),
        {"document_type": "claim_form", "outcome": "COMPLETE", "summary": "s"},
        {"outcome": "COMPLETE"},
        type("C", (), {"attempt_count": 1})(),
        sqs_visibility(sqs),
    )

    assert decision is consumer_main.Ack.DELETE
    assert s3.puts == [f"reports/{document_id}.json"]


def test_a_report_that_cannot_be_stored_leaves_the_document_retryable(conn, monkeypatch):
    """The status and the report are one step, so a failed put must undo the status write.

    This is the case that has no second chance. Committing COMPLETED and then failing to
    store the report looks survivable, but it is not: the redelivered message finds the
    document terminal, deletes itself, and the report route returns 404 for the life of the
    document. Nothing anywhere retries. So the status write is held open until the report is
    durable, and a failed put rolls it back and returns the message instead.
    """
    from consumer import main as consumer_main
    from consumer.claim import claim as take_claim

    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    s3 = FakeS3(fail_put=True)
    worker = make_worker(monkeypatch, sqs, s3)

    # A real claim, so the fencing token in the finishing write is the real one.
    claimed = take_claim(conn, document_id, 120)

    decision = worker._record(
        conn,
        type("E", (), {"document_id": document_id})(),
        {"document_type": "claim_form", "outcome": "COMPLETE", "summary": "s"},
        {"outcome": "COMPLETE"},
        claimed,
        sqs_visibility(sqs),
    )

    assert decision is consumer_main.Ack.RETURN
    assert sqs.deleted == []
    assert s3.puts == []
    assert status_of(conn, document_id) == "PROCESSING", (
        "a document whose report was never stored must not be left terminal, "
        "because nothing would ever retry it"
    )


def test_a_stored_report_commits_the_completion(conn, monkeypatch):
    """The other half of the atomic pair, with nothing stubbed out.

    The two tests above both replace mark_completed, so neither would notice if the status
    write were held open and never committed. That failure would leave every document
    stuck in PROCESSING forever, so it is worth one test that runs the real conditional
    write, the real put, and then looks at the row.
    """
    from consumer import main as consumer_main
    from consumer.claim import claim as take_claim

    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    s3 = FakeS3()
    worker = make_worker(monkeypatch, sqs, s3)
    claimed = take_claim(conn, document_id, 120)

    decision = worker._record(
        conn,
        type("E", (), {"document_id": document_id})(),
        {"document_type": "claim_form", "outcome": "COMPLETE", "summary": "s"},
        {"outcome": "COMPLETE"},
        claimed,
        sqs_visibility(sqs),
    )

    assert decision is consumer_main.Ack.DELETE
    assert s3.puts == [f"reports/{document_id}.json"]
    # Read on a second connection, so a transaction left open on the first cannot hide it.
    with psycopg.connect(DSN) as other:
        assert status_of(other, document_id) == "COMPLETED"


def test_a_failed_document_whose_report_cannot_be_stored_is_also_retryable(conn, monkeypatch):
    """The FAILED branch gets the same treatment, and it needs its own test.

    A half committed FAILED row with no report has the same dead end as a COMPLETED one: the
    redelivery finds it terminal and deletes the message. So the rollback has to apply here
    too, even though the eventual outcome is a failure either way.
    """
    from consumer import main as consumer_main
    from consumer.claim import claim as take_claim

    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    s3 = FakeS3(fail_put=True)
    worker = make_worker(monkeypatch, sqs, s3)
    claimed = take_claim(conn, document_id, 120)

    decision = worker._record(
        conn,
        type("E", (), {"document_id": document_id})(),
        {"outcome": "FAILED", "error": {"message": "the file could not be read"}},
        {"outcome": "FAILED"},
        claimed,
        sqs_visibility(sqs),
    )

    assert decision is consumer_main.Ack.RETURN
    assert sqs.deleted == []
    assert s3.puts == []
    with psycopg.connect(DSN) as other:
        assert status_of(other, document_id) == "PROCESSING"


def test_the_visibility_timeout_is_extended_before_the_report_is_written(conn, monkeypatch):
    """The heartbeat stops when the graph finishes, so the writes after it run unprotected.

    Whatever the last beat bought is all the window there is, and a put_object that botocore
    retries can outlast it. The message is then redelivered while this transaction still
    holds the row lock. That costs a delivery attempt rather than any data, because the
    redelivered worker waits on the lock and then finds the document terminal, but three of
    those put a perfectly finished document into the dead letter queue.
    """
    from consumer.claim import claim as take_claim
    from consumer.config import CONSUMER_CONFIG

    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    s3 = FakeS3()
    worker = make_worker(monkeypatch, sqs, s3)
    claimed = take_claim(conn, document_id, 120)

    # One list, so the assertion is about the order of the two calls and not just that both
    # happened. Two separate counters would pass with the extension after the put.
    order: list[str] = []
    real_put = s3.put_object
    monkeypatch.setattr(s3, "put_object", lambda **kw: (order.append("put"), real_put(**kw))[1])

    worker._record(
        conn,
        type("E", (), {"document_id": document_id})(),
        {"document_type": "claim_form", "outcome": "COMPLETE", "summary": "s"},
        {"outcome": "COMPLETE"},
        claimed,
        lambda seconds: order.append(f"visibility:{seconds}"),
    )

    assert order == [f"visibility:{CONSUMER_CONFIG.lease_seconds}", "put"]


def test_a_failed_visibility_extension_rolls_back_and_returns_the_message(conn, monkeypatch):
    """The extension is a write like any other, and it runs with a transaction open.

    An SQS throttle here used to escape every frame up to the poll loop, taking the worker
    down with an uncommitted terminal status still open. The document survived that, because
    the dying connection rolled back, but a transient throttle should not cost a task
    restart. It is inside the rollback now, so it returns the document instead.
    """
    from consumer import main as consumer_main
    from consumer.claim import claim as take_claim

    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    s3 = FakeS3()
    worker = make_worker(monkeypatch, sqs, s3)
    claimed = take_claim(conn, document_id, 120)

    def throttled(_: int) -> None:
        raise RuntimeError("Throttled: rate exceeded")

    decision = worker._record(
        conn,
        type("E", (), {"document_id": document_id})(),
        {"document_type": "claim_form", "outcome": "COMPLETE", "summary": "s"},
        {"outcome": "COMPLETE"},
        claimed,
        throttled,
    )

    assert decision is consumer_main.Ack.RETURN
    assert s3.puts == [], "nothing may be written once the window could not be secured"
    with psycopg.connect(DSN) as other:
        assert status_of(other, document_id) == "PROCESSING"


def test_an_unexpected_error_leaves_the_message_and_keeps_the_loop_running(conn, monkeypatch):
    """The shape behind the finding above, rather than the one instance of it.

    Every AWS and database call in _process can raise on a transient fault, and each one used
    to end the poll loop. The message is left alone so it returns after the visibility
    timeout, and the worker carries on with the next one.
    """
    from consumer import main as consumer_main

    document_id = insert(conn, "QUEUED")
    sqs = FakeSQS([])
    worker = make_worker(monkeypatch, sqs, FakeS3())

    def boom(*_: Any, **__: Any):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(consumer_main, "claim", boom)

    worker._handle(message(document_id))

    assert sqs.deleted == [], "an error of unknown cause must not destroy the only copy"
    assert status_of(conn, document_id) == "QUEUED"
