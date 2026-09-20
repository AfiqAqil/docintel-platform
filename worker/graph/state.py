"""The shared graph state.

One TypedDict, passed through every node. Nodes return only the keys they change; LangGraph
merges each return into the running state. `trace` is the only accumulating key, so it is the
only one with a reducer. Everything else is last write wins, which is what we want when a
retry overwrites an earlier extraction.
"""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict


class DocType(StrEnum):
    """The classification enum. The model is constrained to these values."""

    CLAIM_FORM = "claim_form"
    POLICY_DOCUMENT = "policy_document"
    INVOICE = "invoice"
    IDENTITY_DOCUMENT = "identity_document"
    CUSTOMER_CORRESPONDENCE = "customer_correspondence"
    SUPPORTING_EVIDENCE = "supporting_evidence"
    UNKNOWN = "unknown"


class Outcome(StrEnum):
    """How processing went, as distinct from where it got to.

    Kept separate from the document's status so that "completed, but information is
    missing" is not modelled as a failure.
    """

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNSUPPORTED = "UNSUPPORTED"


class ErrorKind(StrEnum):
    """Why a node failed, and therefore what the consumer should do about it.

    TERMINAL means the document cannot be processed however many times we try: an encrypted
    PDF stays encrypted. The graph records the failure and finishes normally.

    TRANSIENT means the attempt failed but the work is still valid: a throttled model call, a
    timeout. These propagate out of the graph so that SQS redelivers the message. They are
    deliberately not a graph route, because retrying is the queue's job, not the graph's.
    """

    TERMINAL = "terminal"
    TRANSIENT = "transient"


class StepRecord(TypedDict):
    """One entry in the trace, appended by every node as it completes."""

    node: str
    status: Literal["ok", "error"]
    duration_ms: int
    detail: str | None


class GraphError(TypedDict):
    node: str
    kind: ErrorKind
    message: str


class FieldValue(TypedDict):
    """One extracted field, with the evidence it came from.

    `snippet` is the span of source text the model says it took the value from, and
    `verified` records whether that span was actually found in the source. A snippet that
    cannot be found is a fabricated citation, so the field is rejected. This is why fields
    carry snippets at all.

    `verified` is None when there was nothing to check against, which is the image only case:
    a scanned PDF or a photograph has no extracted text to match a snippet to.
    """

    value: str | None
    snippet: str | None
    verified: bool | None


class State(TypedDict, total=False):
    """Everything the graph knows about one document."""

    # Set by the consumer from the SQS message, before the graph runs.
    document_id: str
    s3_key: str
    content_type: str
    file_size: int
    filename: str

    # The original file's bytes. The consumer fetches them from S3 and puts them here, so
    # the graph itself makes no AWS call of any kind and can run in tests from a local file.
    # Fetching is infrastructure work, which is the consumer's layer, not the graph's.
    # These bytes are never persisted or logged, and there is no checkpointer to write them
    # to; see architecture section 14 on why the graph re-runs rather than checkpointing.
    raw_bytes: bytes

    # load_document
    text: str
    page_images: list[bytes]
    # True when there is no usable text layer, so snippet verification cannot run and the
    # model is given images instead.
    image_only: bool

    # classify
    doc_type: DocType
    confidence: float
    classification_notes: str

    # the extraction nodes
    extracted: dict[str, FieldValue]
    extraction_attempts: int

    # validate
    missing_fields: list[str]
    validation_errors: list[str]

    # routers and mark_unsupported
    outcome: Outcome

    # generate_report
    report: dict[str, Any]

    # any node
    error: GraphError | None

    # Free text notes for the report, things a reviewer should know: a page cap that was
    # hit, snippet verification that was skipped, a retry that happened.
    observations: Annotated[list[str], operator.add]

    # Appended by every node. The only accumulating key, so the only one with a reducer.
    trace: Annotated[list[StepRecord], operator.add]

    # The trace with sensitive values scrubbed out, written once by mask_pii and used by
    # generate_report. It is a separate key rather than an overwrite of `trace` because the
    # reducer above makes `trace` append only: a node returning `trace` adds to it and can
    # never replace it. `trace` stays in memory for the duration of the run and is never
    # persisted or logged; `masked_trace` is what reaches the report.
    masked_trace: list[StepRecord]
