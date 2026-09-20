"""JSON logging with `document_id` in the context.

S3 event notifications cannot carry custom message attributes, so `document_id` is the
correlation id every service threads through by hand (ARCHITECTURE section 7). A contextvar
carries it for the lifetime of one request without passing it through every function
signature, and each request gets its own copy because Starlette runs each request in its own
asyncio task.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from typing import Any

_document_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "document_id", default=None
)


def set_document_id(document_id: str | None) -> None:
    _document_id.set(document_id)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "document_id": _document_id.get(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
