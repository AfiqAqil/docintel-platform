"""The poll loop. Infrastructure event handling, kept out of the graph entirely.

This module owns everything the graph deliberately does not: the SQS long poll, the claim,
the lease and visibility heartbeat, the ack decision, the reaper, and the status writes. The
graph takes state in and returns state out, which is what lets it be tested against a fake
with no AWS and no database.
"""

from __future__ import annotations

import json
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
import psycopg

from consumer import logging as consumer_logging
from consumer.claim import Claim, claim, mark_completed, mark_failed, record_step
from consumer.config import CONSUMER_CONFIG as CFG
from consumer.heartbeat import Heartbeat
from consumer.messages import Ack, DocumentEvent, S3TestEvent, UnparseableMessage, parse
from consumer.reaper import reap
from graph.build import build_graph
from graph.state import Outcome

log = consumer_logging.configure()


def _boto(service: str) -> Any:
    """One boto3 client.

    endpoint_url is passed as None rather than omitted when it is unset, which is what
    boto3 already treats as "use the real endpoint". Building a kwargs dict instead would
    read the same and defeat every one of boto3's per service typed overloads.
    """
    return boto3.client(  # type: ignore[call-overload]
        service, region_name=CFG.aws_region, endpoint_url=CFG.aws_endpoint_url
    )


def _dsn() -> str:
    if CFG.database_dsn:
        return CFG.database_dsn
    password = _db_password()
    return (
        f"host={CFG.db_host} port={CFG.db_port} dbname={CFG.db_name} "
        f"user={CFG.db_user} password={password}"
    )


def _db_password() -> str:
    import os

    if not CFG.db_secret_arn:
        return os.environ.get("DB_PASSWORD", "docintel")
    raw = _boto("secretsmanager").get_secret_value(SecretId=CFG.db_secret_arn)["SecretString"]
    try:
        return str(json.loads(raw)["password"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return raw


def connect() -> psycopg.Connection:
    return psycopg.connect(_dsn())


def wait_for_schema(timeout_seconds: int = 120) -> None:
    """Block until the documents table exists.

    ECS gives no ordering guarantee between the API task that runs the migration and this
    one. Crashing on a missing table would work eventually, through the restart loop, but it
    would trip the deployment circuit breaker on a cold start and roll back a deploy that
    was fine.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM documents LIMIT 1")
            log.info("schema is present")
            return
        except Exception as exc:
            if time.monotonic() > deadline:
                raise
            log.info("waiting for the schema: %s", exc)
            time.sleep(3)


class Worker:
    def __init__(self) -> None:
        self.sqs = _boto("sqs")
        self.s3 = _boto("s3")
        self.graph = build_graph()
        self.running = True
        self._last_reap = 0.0

    # -- shutdown ----------------------------------------------------------------------

    def request_stop(self, *_: object) -> None:
        """SIGTERM from ECS. Stop polling, but finish the message in flight.

        Dropping it would be safe, since SQS would redeliver, but it would waste every model
        call already made for that document.
        """
        log.info("stop requested, finishing the message in flight")
        self.running = False

    # -- the loop ----------------------------------------------------------------------

    def run(self) -> None:
        wait_for_schema()
        log.info("polling %s", CFG.queue_url)

        while self.running:
            self._touch_heartbeat_file()
            self._maybe_reap()

            response = self.sqs.receive_message(
                QueueUrl=CFG.queue_url,
                MaxNumberOfMessages=CFG.max_messages,
                WaitTimeSeconds=CFG.wait_time_seconds,
                AttributeNames=["ApproximateReceiveCount"],
            )

            for message in response.get("Messages", []):
                self._handle(message)

    def _touch_heartbeat_file(self) -> None:
        """The container health check reads this. It proves the loop is turning, which a
        process liveness check does not: a wedged poll loop is still a running process."""
        Path(CFG.heartbeat_file).write_text(str(time.time()))

    def _maybe_reap(self) -> None:
        if time.monotonic() - self._last_reap < CFG.reap_interval_seconds:
            return
        self._last_reap = time.monotonic()
        try:
            with connect() as conn:
                result = reap(conn, CFG.processing_grace_seconds, CFG.upload_expiry_seconds)
            if result.total:
                log.warning(
                    "reaped %d stuck and %d abandoned rows",
                    result.stuck_processing,
                    result.abandoned_uploads,
                )
        except Exception:
            # The reaper is a safety net, not the main path. A failure here must not stop
            # the loop from processing documents.
            log.exception("reaper failed")

    # -- one message -------------------------------------------------------------------

    def _handle(self, message: dict[str, Any]) -> None:
        receipt = message["ReceiptHandle"]
        receive_count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", 1))

        try:
            event = parse(message["Body"], CFG.upload_prefix)
        except S3TestEvent:
            log.info("ignoring s3:TestEvent")
            self._delete(receipt)
            return
        except (UnparseableMessage, ValueError):
            # Redelivering this cannot change the outcome, and leaving it would block the
            # queue for the length of the redrive window on every delivery.
            log.exception("unparseable message, deleting")
            self._delete(receipt)
            return

        with consumer_logging.document_context(str(event.document_id)):
            decision = self._process(event, receipt, receive_count)
            if decision is Ack.DELETE:
                self._delete(receipt)
            else:
                log.info("leaving the message to time out and return")

    def _process(self, event: DocumentEvent, receipt: str, receive_count: int) -> Ack:
        last_attempt = receive_count >= CFG.max_receive_count

        with connect() as conn:
            claimed = claim(conn, event.document_id, CFG.lease_seconds)

            if not isinstance(claimed, Claim):
                # The ack rule that matters. Terminal means COMPLETED or FAILED and nothing
                # else: only then is the work provably finished and the message safe to
                # delete. Anything else is left to time out, because deleting it would
                # destroy the only copy of the work if the current owner turns out to be
                # dead.
                if claimed.terminal:
                    log.info("already finished as %s, deleting the message", claimed.status)
                    return Ack.DELETE
                if claimed.missing:
                    # An object with no row. No amount of redelivery will create one.
                    log.warning("no database row for this object, deleting the message")
                    return Ack.DELETE
                log.info("held by another worker (%s), returning the message", claimed.status)
                return Ack.RETURN

            log.info("claimed, attempt %d of %d", receive_count, CFG.max_receive_count)

            try:
                body = self.s3.get_object(Bucket=event.bucket, Key=event.s3_key)["Body"].read()
            except Exception:
                log.exception("could not fetch the object")
                if last_attempt:
                    mark_failed(
                        conn,
                        event.document_id,
                        "The uploaded file could not be read",
                        attempt_count=claimed.attempt_count,
                    )
                    return Ack.DELETE
                return Ack.RETURN

            return self._run_graph(conn, event, body, receipt, last_attempt, claimed)

    def _run_graph(
        self,
        conn: psycopg.Connection,
        event: DocumentEvent,
        body: bytes,
        receipt: str,
        last_attempt: bool,
        claimed: Claim,
    ) -> Ack:
        def extend_visibility(seconds: int) -> None:
            self.sqs.change_message_visibility(
                QueueUrl=CFG.queue_url, ReceiptHandle=receipt, VisibilityTimeout=seconds
            )

        initial = {
            "document_id": str(event.document_id),
            "s3_key": event.s3_key,
            "filename": event.s3_key,
            "content_type": self._content_type(event),
            "file_size": event.size,
            "raw_bytes": body,
        }

        with Heartbeat(
            connect,
            extend_visibility,
            event.document_id,
            CFG.lease_seconds,
            CFG.heartbeat_seconds,
            claimed.attempt_count,
        ) as beat:
            try:
                final: dict[str, Any] = {}
                # stream, not invoke, so current_step can be written as each node finishes.
                for chunk in self.graph.stream(initial, stream_mode="updates"):
                    for node, update in chunk.items():
                        # Written immediately rather than left to the next heartbeat. The
                        # heartbeat runs every 30 seconds and most documents finish inside
                        # that, so deferring it meant current_step was almost always null
                        # and the progress the frontend promises never appeared.
                        record_step(conn, event.document_id, claimed.attempt_count, node)
                        beat.set_step(node)
                        final.update(update)
                    if beat.lease_lost:
                        # Someone else owns this document now. Stopping here wastes the
                        # remaining work, which is cheaper than racing them to the write.
                        log.warning("lease lost mid run, abandoning")
                        return Ack.RETURN
            except Exception as exc:
                # A transient error left the graph on purpose, so that the queue retries
                # rather than the document being marked failed while it is still in flight.
                log.exception("graph raised, treating as transient")
                if last_attempt:
                    # Out of attempts. Record it, so the document does not sit in
                    # PROCESSING until the reaper notices.
                    mark_failed(
                        conn,
                        event.document_id,
                        f"{type(exc).__name__}: {exc}",
                        attempt_count=claimed.attempt_count,
                    )
                    return Ack.DELETE
                return Ack.RETURN

        report = final.get("report") or {}
        return self._record(conn, event, report, final, claimed)

    def _record(
        self,
        conn: psycopg.Connection,
        event: DocumentEvent,
        report: dict[str, Any],
        final: dict[str, Any],
        claimed: Claim,
    ) -> Ack:
        doc_type = str(report.get("document_type") or "") or None

        # Ordering matters here and it is not obvious.
        #
        # The conditional database write is the thing that decides ownership: it succeeds
        # only for the worker that still holds the lease. Writing the report to S3 before it
        # meant a worker that had already lost the lease still overwrote the winner's report
        # and only then discovered its own database write was refused. The row would then
        # describe the winner's run while the stored report was the loser's, with nothing
        # anywhere reporting an error.
        #
        # So the database write goes first, and the report is only published once ownership
        # is settled. A worker that has lost the lease now writes nothing at all.

        if report.get("outcome") == "FAILED":
            message = (report.get("error") or {}).get("message", "processing failed")
            if not mark_failed(
                conn, event.document_id, message, claimed.attempt_count, doc_type
            ):
                log.warning("lease lost before the failure could be recorded, discarding it")
                return Ack.RETURN
            self._publish_report(event.document_id, report)
            log.info("recorded FAILED")
            return Ack.DELETE

        outcome = str(final.get("outcome") or report.get("outcome") or Outcome.INCOMPLETE)
        summary = json.dumps(
            {
                "summary": report.get("summary"),
                "missing_information": report.get("missing_information") or [],
                "validation_errors": report.get("validation_errors") or [],
                "observations": report.get("observations") or [],
            }
        )

        if not mark_completed(
            conn, event.document_id, outcome, doc_type, summary, claimed.attempt_count
        ):
            # The lease was taken between the last heartbeat and this write. The new owner
            # will produce its own result, so this one is dropped rather than forced in, and
            # crucially nothing has been written to S3 yet.
            log.warning("lease lost before the result could be written, discarding it")
            return Ack.RETURN

        self._publish_report(event.document_id, report)
        log.info("recorded COMPLETED as %s", outcome)
        return Ack.DELETE

    # -- helpers -----------------------------------------------------------------------

    def _content_type(self, event: DocumentEvent) -> str:
        """Read the content type back off the object, since the event does not carry it."""
        try:
            return str(
                self.s3.head_object(Bucket=event.bucket, Key=event.s3_key).get(
                    "ContentType", "application/octet-stream"
                )
            )
        except Exception:
            return "application/octet-stream"

    def _publish_report(self, document_id: uuid.UUID, report: dict[str, Any]) -> None:
        """Write the report, after ownership has already been settled by the database write.

        A failure here leaves the document with a correct status and no report, which the
        API surfaces as a 404 on the report route. That is a visible, honest degradation.
        The alternative, publishing before the ownership check, traded it for a silent one:
        a stored report belonging to a different run than the row describing it. A wrong
        answer nobody can detect is worse than a missing one everybody can.

        It is not retried and the message is still acked, because the document is finished
        as far as status is concerned, and redelivery would find it terminal and ack it
        anyway without reaching this line.
        """
        try:
            self.s3.put_object(
                Bucket=CFG.s3_bucket,
                Key=f"{CFG.report_prefix}{document_id}.json",
                Body=json.dumps(report, indent=2, default=str).encode(),
                ContentType="application/json",
            )
        except Exception:
            log.exception("status was recorded but the report could not be stored")

    def _delete(self, receipt: str) -> None:
        self.sqs.delete_message(QueueUrl=CFG.queue_url, ReceiptHandle=receipt)


def main() -> int:
    worker = Worker()
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    worker.run()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
