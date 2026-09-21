"""JSON logging with the document id bound for a whole message.

document_id is the correlation id across all three services and it is also the S3 key, so a
single identifier follows a document from upload to report. S3 event notifications cannot
carry custom message attributes, so the consumer parses the id out of the key and binds it
here for the lifetime of the message rather than threading it through every call.
"""

from __future__ import annotations

import contextvars
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

_document_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "document_id", default=None
)


class DocumentIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.document_id = _document_id.get() or "-"
        return True


@contextmanager
def document_context(document_id: str) -> Iterator[None]:
    token = _document_id.set(document_id)
    try:
        yield
    finally:
        _document_id.reset(token)


def configure() -> logging.Logger:
    from pythonjsonlogger.json import JsonFormatter

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        # The offset is explicit so a reader never has to guess which zone a line is in.
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(document_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(DocumentIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # boto3 logs every API call at INFO, which buries our own lines.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    return logging.getLogger("worker")
