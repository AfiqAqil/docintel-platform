"""Reading the S3 event notification, and the ack decision.

Both are small and both are easy to get wrong in ways that only show up under load, so they
live here on their own rather than inside the poll loop.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote_plus


@dataclass(frozen=True)
class DocumentEvent:
    document_id: uuid.UUID
    s3_key: str
    bucket: str
    size: int


class S3TestEvent(Exception):
    """The s3:TestEvent S3 sends once when a notification is configured.

    It carries no records and is not a document. Acked and ignored; without this the
    consumer would log a parse failure every time the infrastructure is recreated.
    """


class UnparseableMessage(Exception):
    """Not something this consumer can ever process, however many times it is redelivered."""


def parse(body: str, upload_prefix: str) -> DocumentEvent:
    payload = json.loads(body)

    if payload.get("Event") == "s3:TestEvent":
        raise S3TestEvent

    records = payload.get("Records")
    if not records:
        raise UnparseableMessage("message has no Records")

    record = records[0]
    try:
        bucket = record["s3"]["bucket"]["name"]
        raw_key = record["s3"]["object"]["key"]
        size = int(record["s3"]["object"].get("size", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise UnparseableMessage(f"not an S3 object notification: {exc}") from exc

    # S3 URL encodes the key in the notification, and encodes spaces as "+" rather than
    # "%20", so unquote alone is not enough. This is exactly why the key carries no filename:
    # the document id is the key, and the filename is a column.
    key = unquote_plus(raw_key)

    if not key.startswith(upload_prefix):
        # The notification is filtered to the uploads prefix, so this should be
        # unreachable. It is checked anyway, because the alternative if the filter is ever
        # misconfigured is the worker triggering on its own report writes into the same
        # bucket, in a loop.
        raise UnparseableMessage(f"key {key!r} is outside the upload prefix")

    suffix = key[len(upload_prefix) :]
    try:
        document_id = uuid.UUID(suffix)
    except ValueError as exc:
        raise UnparseableMessage(f"key suffix {suffix!r} is not a document id") from exc

    return DocumentEvent(document_id=document_id, s3_key=key, bucket=bucket, size=size)


class Ack(StrEnum):
    """What to do with the message once the attempt is over.

    DELETE removes it. The work is finished, or provably will never finish.

    RETURN leaves it to time out and come back. This is the safe default whenever the
    outcome is unknown, because deleting a message destroys the only copy of the work: if
    the current owner is dead, nothing will ever process that document again.
    """

    DELETE = "delete"
    RETURN = "return"
